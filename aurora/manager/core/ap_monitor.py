import atexit
import collections
import datetime
import logging
import json
from pprint import pformat
import sys
import threading
import time
import traceback
from types import *
import uuid
import weakref

import MySQLdb as mdb

from cls_logger import get_cls_logger
import ap_provision.http_srv as provision
#import dispatcher

LOGGER = logging.getLogger(__name__)


class APMonitor(object):

    sql_locked = None
    SLEEP_TIME = 45
    # TODO: Make function to determine if dispatcher still exists
    def __init__(self, dispatcher, aurora_db, host, username, password):
        self.LOGGER = get_cls_logger(self)
        # Configure dispatcher
        self.dispatcher = dispatcher
        self.dispatcher.set_timeout_callback(self.timeout)
        self.dispatcher.set_response_callback(self.process_response)
        self.dispatcher.start_connection()

        #self.dispatcher_ref = weakref.ref(dispatcher)
        #self.LOGGER.debug("Made weak ref %s %s",self.dispatcher_ref, self.dispatcher_ref())

        self.aurora_db = aurora_db
        # self.ut = UptimeTracker(host, username, password)
        self.poller_threads = {}

        # To handle incoming status update requests, make a command queue
        self.timeout_queue = collections.deque()
        self._make_queue_daemon()

        #Connect to Aurora mySQL Database
        self.LOGGER.info("Connecting to SQLdb...")
        try:
            self.con = mdb.connect(host, username, password, 'aurora')
            APMonitor.sql_locked = False
        except mdb.Error, e:
            self.LOGGER.error("Error %d: %s" % (e.args[0], e.args[1]))
            sys.exit(1)

        atexit.register(self._closeSQL)

    def _closeSQL(self):
        self.LOGGER.info("Closing SQL connection...")
        self.aurora_db.ap_status_unknown()
        if self.con:
            self.con.close()
        else:
            self.LOGGER.info('Connection already closed!')

    def _make_queue_daemon(self):
        self.LOGGER.info("Creating Queue Daemon...")
        self.qd = StoppableThread(target=self._watch_queue)
        self.qd.start()

    def _watch_queue(self, stop_event=None):
        while True:
            while len(self.timeout_queue) < 1 and not stop_event.is_set():
                time.sleep(1)
            if stop_event.is_set():
                self.LOGGER.info("Queue Daemon caught stop event")
                break
            (args, kwargs) = self.timeout_queue.popleft()
            self._set_status(*args, **kwargs)

    def _add_call_to_queue(self, *args, **kwargs):
        self.timeout_queue.append((args, kwargs))

    def stop(self):
        self._close_all_poller_threads()
        self.qd.stop()

    def _close_all_poller_threads(self):
        self.LOGGER.debug("Closing all threads %s", self.poller_threads)
        for ap_name in self.poller_threads.keys():
            self._close_poller_thread(ap_name, 'admin')

    def _close_poller_thread(self, ap_name, unique_id):
        if ap_name in self.poller_threads and unique_id == 'admin':
            poller_thread = self.poller_threads.pop(ap_name)
            self.LOGGER.debug("Stopping thread %s %s", ap_name, poller_thread)
            poller_thread.stop()

    def process_response(self, channel, method, props, body):
        """Processes any responses it sees, checking to see if the
        correlation ID matches one sent.  If it does, the response
        is displayed along with the request originally sent."""

        # Basic Proof-of-Concept Implementation
        # 1. We dispatch (see method above)
        # 2. Response received: if related to a request we sent out, OK
        # ACK it
        # Update database to reflect content (i.e. success or error)

        # If we don't have a record, that means that we already
        # handled a timeout previously and something strange happened to the AP
        # to cause it to wait so long. Reset it.

        # Check if we have a record of this ID
        have_request = False
        entry = None
        self.LOGGER.info("Receiving...")
        if self.dispatcher.lock:
            self.LOGGER.info("Locked, waiting...")
            while self.dispatcher.lock:
                time.sleep(0.1)
                pass

        self.LOGGER.debug("channel: %s",channel)
        self.LOGGER.debug("method: %s", method)
        self.LOGGER.debug(repr(props))
        self.LOGGER.debug(body)
        self.LOGGER.debug("\nrequests_sent: %s",self.dispatcher.requests_sent)

        # Decode response
        decoded_response = json.loads(body)
        message = decoded_response['message']
        ap_name = decoded_response['ap']
        config = decoded_response['config']
        region = config['region']
        if message == 'SYN':
            #TODO: If previous message has been dispatched and we are waiting 
            #      for a response, cancel the timer and/or send the command again
            # AP has started, check if we need to restart slices
            self.LOGGER.info("%s has connected...", ap_name)
            self.aurora_db.ap_status_up(ap_name)
            self.dispatcher.remove_request(ap_syn=ap_name)
            # Tell ap monitor, let it handle restart of slices
            #self.start_poller(ap_name)
            slices_to_restart = decoded_response['slices_to_restart']
            self.restart_slices(ap_name, slices_to_restart)
            provision.update_last_known_config(ap_name, config)
            self.aurora_db.ap_update_hw_info(config['init_hardware_database'], ap_name, region)
            self.start_poller(ap_name)
            return

        elif message == 'SYN/ACK':
            self.LOGGER.info("%s responded to 'SYN' request", ap_name)
            # Cancel timers corresponding to 'SYN' message
            (have_request, entry) = self.dispatcher._have_request(props.correlation_id)
            if have_request:
                entry[1].cancel()
                self.dispatcher.requests_sent.remove(entry)
            else:
                self.LOGGER.warning("Warning: No request for received 'SYN/ACK' from %s", ap_name)
            provision.update_last_known_config(ap_name, config)
            self.aurora_db.ap_status_up(ap_name)
            self.aurora_db.ap_update_hw_info(config['init_hardware_database'], ap_name, region)

            #ap_slice_list = map(lambda slice_: slice_ in config['init_database'].keys() if slice_ != 'default_slice')
            # self.LOGGER.debug("Test map %s", test_map)
            for ap_slice_id in (ap_slice_id for ap_slice_id in config['init_database'].keys() if ap_slice_id != 'default_slice'):
                self.aurora_db.ap_slice_status_up(ap_slice_id)
            self.start_poller(ap_name)
            return


        elif message == 'FIN':
            self.LOGGER.info("%s is shutting down...", ap_name)
            try:
                self.set_status(None, None, False, ap_name)
                self.aurora_db.ap_update_hw_info(config['init_hardware_database'], ap_name, region)
                self.aurora_db.ap_status_down(ap_name)
                self.LOGGER.info("Updating config files...")
                provision.update_last_known_config(ap_name, config)
            except Exception as e:
                self.LOGGER.error(e.message)
            self.LOGGER.debug("Last known config:")
            self.LOGGER.debug(pformat(config))
            return

        (have_request, entry) = self.dispatcher._have_request(props.correlation_id)

        if have_request is not None:
            # decoded_response = json.loads(body)
            self.LOGGER.debug('Printing received message')
            self.LOGGER.debug(message)

            # Set status, stop timer, delete record
            #print "entry[2]:",entry[2]
            if entry[2] != 'admin':
                self.set_status(entry[2], decoded_response['successful'], ap_name=ap_name)
                self.aurora_db.ap_update_hw_info(config['init_hardware_database'], ap_name, region)

                self.LOGGER.info("Updating config files...")
                provision.update_last_known_config(ap_name, config)
            else:
                if message != "RESTARTING" and message != "AP reset":
                    self.update_records(message["ap_slice_stats"])
                    self.aurora_db.ap_update_hw_info(config['init_hardware_database'], ap_name, region)

                else:
                    #Probably a reset or restart command sent from ap_monitor
                    #Just stop timer and remove entry
                    pass

            self.dispatcher.remove_request(entry[0])

        else:
            self.LOGGER.info("Sending reset to '%s'", ap_name)
            # Reset the access point
            self.reset_AP(ap_name)


        # Regardless of content of message, acknowledge receipt of it
        channel.basic_ack(delivery_tag = method.delivery_tag)

    def timeout(self, ap_slice_id, ap_name, message_uuid = None):
        """This code will execute when a response is not
        received for the command associated with the unique_id
        after a certain time period.  It modifies the database
        to reflect the current status of the AP."""

        if message_uuid is not None:
            # dispatcher = self.dispatcher_ref()
            # if dispatcher is None:
            #     self.LOGGER.warning("Dispatcher has been deallocated")
            # else:
                # dispatcher.remove_request(message_uuid)
            self.dispatcher.remove_request(message_uuid)
        self.LOGGER.debug("%s %s", type(ap_slice_id), ap_slice_id)
        # A timeout is serious: it is likely that
        # the AP's OS has crashed, or at least aurora is
        # no longer running.
        
        #if unique_id != 'admin':
        #    self.set_status(unique_id, success=False, ap_up=False, )
        #else:
        self._add_call_to_queue(ap_slice_id, success=False, ap_up=False, ap_name=ap_name)
        #remove thread from the thread pool
        
        #self._close_poller_thread(ap_name, ap_slice_id)

        # In the future we might do something more with the unique_id besides
        # identifying the AP, like log it to a list of commands that cause
        # AP failure, but for now it's good enough to know that our AP
        # has died and at least this command failed
        # If there are several commands waiting, this will execute several times
        # but all slices should already be marked
        # as deleted, down or failed, so there will not be any issue

    def update_records(self, message):
        """Update the traffic information of ap_slice"""
        self.LOGGER.debug("Updating records...")
        for ap_slice_id in message.keys():
            self.aurora_db.ap_slice_update_time_active(ap_slice_id)
            self.aurora_db.ap_slice_update_bytes_sent(ap_slice_id, message.get(ap_slice_id))

    def set_status(self, unique_id, success, ap_up=True, ap_name=None):
        self._add_call_to_queue(unique_id, success, ap_up, ap_name)

    def _set_status(self, unique_id, success, ap_up=True, ap_name=None):
        """Sets the status of the associated request in the
        database based on the previous status, i.e. pending -> active if
        create slice, deleting -> deleted if deleting a slice, etc.
        If the ap_up variable is false, the access point
        is considered to be offline and in an unknown state,
        so *all* slices are marked as such (down, failed, etc.)."""

        # DEBUG
        if unique_id != 'SYN':
            self.LOGGER.info("Updating ap status for ID %s.", str(unique_id))
        else:
            self.LOGGER.info("Updating ap status for ID %s.", str(ap_name))
        self.LOGGER.info("Request successful: %s", str(success))
        self.LOGGER.info("Access Point up: %s", str(ap_up))

        if APMonitor.sql_locked:
            self.LOGGER.info("SQL Access is locked, waiting...")
            while APMonitor.sql_locked:
                time.sleep(0.1)
                pass
        # Code:
        # Identify slice by unique_id
        # if ap_up:
        #   if pending and success, mark active
        #   else if deleting and success, mark deleted
        #   else if pending and failed, mark failed
        #   else if deleting and failed, mark failed (forcing user
        # to try deleting again or contact admin saying I can't delete;
        # this situation is so unlikely that if it happens an admin
        # really should come by and see what's going on)
        # else :
        # for all slices and/or commands relating to AP:
        #   if slice is active, mark down
        #   else if slice is deleting, mark deleted (will be when we reinitialize)
        #   else if slice is pending, mark failed
        try:
            # Access point is up - we are receiving individual packets
            if ap_up:
                self.aurora_db.ap_status_up(ap_name)
                self.aurora_db.ap_up_slice_status_update(unique_id, success)

            # Access point down, mark all slices and failed/down
            else:
                if ap_name is None:
                    ap_name = self.aurora_db.get_wslice_physical_ap(ap_slice_id)
                self.aurora_db.ap_status_down(ap_name)
                self._close_poller_thread(ap_name, 'admin')
                self.aurora_db.ap_down_slice_status_update(ap_name)
        except Exception, e:
            self.LOGGER.error(str(e))
        finally:
            APMonitor.sql_locked = False
        return True

    def restart_slices(self, ap, slice_list):
        if APMonitor.sql_locked:
            self.LOGGER.info("SQL Access is locked, waiting...")
            while APMonitor.sql_locked:
                time.sleep(0.1)
                pass
        try:
            APMonitor.sql_locked = True
            for ap_slice_id in slice_list:
                user_id = self.aurora_db.get_user_for_active_ap_slice(ap_slice_id)
                self.LOGGER.debug("Returned user id %s", user_id)
                if user_id is not None:
                    assert type(user_id) is IntType
                    self.LOGGER.info("%s %s for tenant %s", ap_slice_id, status, user_id)
                    self.LOGGER.info("Restarting %s", ap_slice_id)
                    self.dispatcher.dispatch({'slice': ap_slice_id,
                                              'command': 'restart_slice',
                                              'user': user_id
                                             },
                                             ap)
                else:
                    raise Exception("No active slice %s" % slice_id)
        except Exception, e:
            self.LOGGER.error("Error %s", e)
        finally:
            APMonitor.sql_locked = False

    def start_poller(self, ap_name):
        
        #poller_thread = thread(ThreadClass, self)
        poller_thread = TimerThread(target=self.poll_AP, args=(ap_name,))
        self.LOGGER.debug("Starting poller on thread %s", poller_thread)
        self.poller_threads[ap_name] = poller_thread
        poller_thread.start()

    def poll_AP(self, ap_name, stop_event=None):
        #print "Timeout from Dispatcher", self.dispatcher.TIMEOUT
        own_thread = self.poller_threads[ap_name]
        while ap_name in self.poller_threads:
            #time.sleep(APMonitor.SLEEP_TIME)
            self.LOGGER.debug("%s thread is %s", ap_name, own_thread)
            self.get_stats(ap_name)
            # dispatcher = self.dispatcher_ref()
            for i in range(self.dispatcher.TIMEOUT + 5):
                if stop_event.is_set():
                    self.LOGGER.debug("Caught stop event for %s", own_thread)
                    break
                time.sleep(1)
            if stop_event.is_set():
                self.LOGGER.debug("Poller thread for %s is dying now" % ap_name)
                break

    def reset_AP(self, ap):
        """Reset the access point.  If there are serious issues, however,
        a restart may be required."""

        # The unique ID is fixed to be all F's for resets/restarts.
        self.dispatcher.dispatch( { 'slice' : 'admin', 'command' : 'reset' } , ap)

    def restart_AP(self, ap):
        """Restart the access point, telling the OS to reboot."""

        # The unique ID is fixed to be all F's for resets/restarts.
        self.dispatcher.dispatch( { 'slice' : 'admin', 'command' : 'restart' } , ap)

    def get_stats(self, ap):
        """Update the access point """

        # The unique ID is fixed to be all F's
        self.dispatcher.dispatch( { 'slice' : 'admin', 'command' : 'get_stats'}, ap)

    def get_time_format(self, time):
        time = time.total_seconds()
        hours = int(time // 3600)
        time = time - hours * 3600
        minutes = int(time // 60)
        time = time - minutes * 60
        seconds = int(time)
        time_format = str(hours) + ':' + str(minutes) + ':' + str(seconds)
        return time_format

class StoppableThread(threading.Thread):
    """Thread class with a stop method to terminate timers
    that have been started"""
    def __init__(self, *args, **kwargs):
        kwargs = self.add_stop_argument(kwargs)
        super(StoppableThread, self).__init__(*args, **kwargs)

        self.LOGGER = get_cls_logger(self)
        self.LOGGER.debug("__init__ parent thread")
        self.LOGGER.debug(self)

    def add_stop_argument(self, kwargs):
        if 'kwargs' not in kwargs.keys():
            kwargs['kwargs'] = {}
        self._stop = threading.Event()
        kwargs['kwargs']['stop_event'] = self._stop
        return kwargs

    def stop(self):
        self._stop.set()
        #self.join()

    def stopped():
        return self._stop.is_set()

class TimerThread(StoppableThread):
    pass

#for test
#if __name__ == '__main__':
#    host = 'localhost'
#    mysql_username = 'root'
#    mysql_password = 'supersecret'
#    manager = APMonitor(None, host , mysql_username, mysql_password)
#    manager.set_status(12, True)
