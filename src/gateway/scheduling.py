# Copyright (C) 2016 OpenMotics BVBA
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
The scheduling module contains the SchedulingController, this controller is used for scheduling various actions
"""

import sqlite3
import logging
import time
import pytz
from datetime import datetime
from croniter import croniter
from random import randint
from threading import Thread
try:
    import json
except ImportError:
    import simplejson as json

LOGGER = logging.getLogger('openmotics')


class Schedule(object):

    timezone = None

    def __init__(self, id, start, repeat, duration, end, schedule_type, arguments, status):
        self.id = id
        self.start = start
        self.repeat = repeat
        self.duration = duration
        self.end = end
        self.type = schedule_type
        self.arguments = arguments
        self.status = status
        self.last_executed = None
        self.next_execution = None

    @property
    def is_due(self):
        if self.status != 'ACTIVE':
            return False
        if self.repeat is None:
            # Single-run schedules should start on their set starting time if not yet executed
            if self.last_executed is not None:
                return False
            return self.start <= time.time()
        # Repeating
        timezone = pytz.timezone(Schedule.timezone)
        now = datetime.now(timezone)
        cron = croniter(self.repeat, now)
        next_execution = cron.get_next(ret_type=float)
        if self.next_execution is None:
            self.next_execution = next_execution
            return False
        if self.next_execution < time.time():
            self.next_execution = next_execution
            return True
        return False

    @property
    def has_ended(self):
        if self.repeat is None:
            return self.last_executed is not None
        if self.end is not None:
            return self.start + self.end < time.time()
        return False

    def serialize(self):
        return {'id': self.id,
                'start': self.start,
                'repeat': self.repeat,
                'duration': self.duration,
                'end': self.end,
                'type': self.type,
                'arguments': self.arguments,
                'status': self.status,
                'last_executed': self.last_executed,
                'next_execution': self.next_execution}


class SchedulingController(object):
    """
    The SchedulingController controls schedules and executes them. Based on their type, they can trigger different
    behavior.

    Supported types:
    * MIGRATION: Migrates the Master's schedule to here
    * GROUP_ACTION: Executes a Group Action
      * Required arguments: json encoded Group Action id
    * BASIC_ACTION: Executes a Basic Action
      * Required arguments: {'action_type': <action type>,
                             'action_number': <action number>}

    Supported repeats:
    * None: Single execution at start time
    * String: Cron format, docs at https://github.com/kiorky/croniter
    """

    def __init__(self, db_filename, lock, gateway_api):
        """
        Constructs a new ConfigController.

        :param db_filename: filename of the sqlite database used to store the scheduling
        :param lock: DB lock
        :param gateway_api: GatewayAPI
        :type gateway_api: gateway.gateway_api.GatewayApi
        """
        self._gateway_api = gateway_api
        self._lock = lock
        self._connection = sqlite3.connect(db_filename,
                                           detect_types=sqlite3.PARSE_DECLTYPES,
                                           check_same_thread=False,
                                           isolation_level=None)
        self._cursor = self._connection.cursor()
        self._check_tables()
        self._schedules = {}
        self._stop = False
        self._processor = None

        Schedule.timezone = gateway_api.get_timezone()

        self._load_schedule()

    @property
    def schedules(self):
        return self._schedules.values()

    def _execute(self, *args, **kwargs):
        with self._lock:
            try:
                return self._cursor.execute(*args, **kwargs)
            except sqlite3.OperationalError:
                time.sleep(randint(1, 20) / 10.0)
                return self._cursor.execute(*args, **kwargs)

    def _check_tables(self):
        """
        Creates tables and execute migrations
        """
        tables = [table[0] for table in self._execute('SELECT name FROM sqlite_master WHERE type=\'table\';')]
        if 'schedules' not in tables:
            self._execute('CREATE TABLE schedules (id INTEGER PRIMARY KEY, start INTEGER, '
                          'repeat TEXT, duration INTEGER, end INTEGER, type TEXT, arguments TEXT, status TEXT);')
            self._execute('INSERT INTO schedules (start, repeat, duration, end, type, arguments, status) VALUES (?, ?, ?, ?, ?, ?, ?);',
                          (int(time.time()), None, None, None, 'MIGRATION', json.dumps('SCHEDULES'), 'ACTIVE'))

    def _load_schedule(self):
        for row in self._execute('SELECT id, start, repeat, duration, end, type, arguments, status FROM schedules;'):
            schedule_id = row[0]
            self._schedules[schedule_id] = Schedule(id=schedule_id,
                                                    start=row[1],
                                                    repeat=json.loads(row[2]) if row[2] is not None else None,
                                                    duration=row[3],
                                                    end=row[4],
                                                    schedule_type=row[5],
                                                    arguments=json.loads(row[6]) if row[6] is not None else None,
                                                    status=row[7])

    def _update_schedule_status(self, schedule_id, status):
        self._execute('UPDATE schedules SET status = ? WHERE id = ?;', (status, schedule_id))
        self._schedules[schedule_id].status = status

    def remove_schedule(self, schedule_id):
        self._execute('DELETE FROM schedules WHERE id = ?;', (schedule_id,))
        self._schedules.pop(schedule_id, None)

    def add_schedule(self, start, schedule_type, arguments, repeat, duration, end):
        SchedulingController._validate(start, schedule_type, arguments, repeat, duration, end)
        self._execute('INSERT INTO schedules (start, repeat, duration, end, type, arguments, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                      (start,
                       json.dumps(repeat) if repeat is not None else None,
                       duration,
                       end,
                       schedule_type,
                       json.dumps(arguments) if arguments is not None else None,
                       'ACTIVE'))
        self._load_schedule()

    def start(self):
        self._stop = False
        self._processor = Thread(target=self._process)
        self._processor.daemon = True
        self._processor.start()
    
    def stop(self):
        self._stop = True
        
    def _process(self):
        while self._stop is False:
            for schedule in self._schedules.values():
                if schedule.status == 'ACTIVE' and schedule.is_due:
                    thread = Thread(target=self._execute_schedule, args=(schedule,))
                    thread.start()
            now = int(time.time())
            time.sleep(now - now % 60 + 60 - time.time())  # Wait for the next minute mark

    def _execute_schedule(self, schedule):
        """
        :param schedule: Schedule to execute
        :type schedule: gateway.scheduling.Schedule
        """
        try:
            LOGGER.info("Executing schedule {0} with arguments {1}".format(schedule.type, schedule.arguments))

            # Execute
            if schedule.type == 'GROUP_ACTION':
                self._gateway_api.do_group_action(schedule.arguments)
            elif schedule.type == 'BASIC_ACTION':
                self._gateway_api.do_basic_action(**schedule.arguments)
            else:
                LOGGER.warning('Did not process schedule {0}'.format(schedule.type))

            # Cleanup or prepare for next run
            schedule.last_executed = time.time()
            if schedule.has_ended:
                self._update_schedule_status(schedule.id, 'COMPLETED')
        except Exception as ex:
            LOGGER.error('Got error while executing schedule: {0}'.format(ex))
            schedule.last_executed = time.time()

    @staticmethod
    def _validate(start, schedule_type, arguments, repeat, duration, end):
        # Check whether the requested type is valid
        accepted_types = ['GROUP_ACTION', 'BASIC_ACTION']
        if schedule_type not in accepted_types:
            raise RuntimeError('Unknown schedule type. Allowed: {0}'.format(', '.join(accepted_types)))
        # Check duration/repeat/end combinations
        if repeat is None:
            if end is not None:
                raise RuntimeError('No `end` is allowed when it is a non-repeated schedule')
        else:
            if not croniter.is_valid(repeat):
                raise RuntimeError('Invalid `repeat`. Should be a cron-style string. See croniter documentation')
        if duration is not None and duration <= 60:
            raise RuntimeError('If a duration is specified, it should be at least more than 60s')
        # Type specifc checks
        if schedule_type == 'BASIC_ACTION':
            if duration is not None:
                raise RuntimeError('A schedule of type BASIC_ACTION does not has a duration. It is a one-time trigger')
            if not isinstance(arguments, dict) or 'action_type' not in arguments or not isinstance(arguments['action_type'], int) or \
                    'action_number' not in arguments or not isinstance(arguments['action_number'], int) or len(arguments) != 2:
                raise RuntimeError('The arguments of a BASIC_ACTION schedule must be of type dict with arguments `action_type` and `action_number`')
        elif schedule_type == 'GROUP_ACTION':
            if duration is not None:
                raise RuntimeError('A schedule of type GROUP_ACTION does not has a duration. It is a one-time trigger')
            if not isinstance(arguments, int) or arguments < 1 or arguments > 254:
                raise RuntimeError('The arguments of a GROUP_ACTION schedule must be an integer, representing the Group Action to be executed')
