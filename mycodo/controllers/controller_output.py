# coding=utf-8
#
# controller_output.py - Output controller to manage turning outputs on/off
#
#  Copyright (C) 2015-2020 Kyle T. Gabriel <mycodo@kylegabriel.com>
#
#  This file is part of Mycodo
#
#  Mycodo is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mycodo is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mycodo. If not, see <http://www.gnu.org/licenses/>.
#
#  Contact at kylegabriel.com
import datetime
import threading
import time
import timeit

from mycodo.config import SQL_DATABASE_MYCODO
from mycodo.controllers.base_controller import AbstractController
from mycodo.databases.models import Misc
from mycodo.databases.models import Output
from mycodo.databases.models import SMTP
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.modules import load_module_from_file
from mycodo.utils.outputs import output_types
from mycodo.utils.outputs import parse_output_information

MYCODO_DB_PATH = 'sqlite:///' + SQL_DATABASE_MYCODO


class OutputController(AbstractController, threading.Thread):
    """ Class for controlling outputs """
    def __init__(self, ready, debug):
        threading.Thread.__init__(self)
        super(OutputController, self).__init__(ready, unique_id=None, name=__name__)

        self.set_log_level_debug(debug)
        self.control = DaemonControl()

        self.button_pressed = None
        self.button_args_dict = None
        self.button_output_id = None

        # SMTP options
        self.smtp_max_count = None
        self.smtp_wait_time = None
        self.smtp_timer = None
        self.email_count = None
        self.allowed_to_send_notice = None

        self.sample_rate = None
        self.output = {}
        self.dict_outputs = {}
        self.output_unique_id = {}
        self.output_type = {}
        self.output_types = {}

    def initialize_variables(self):
        """ Begin initializing output parameters """
        self.sample_rate = db_retrieve_table_daemon(Misc, entry='first').sample_rate_controller_output

        self.logger.debug("Initializing Outputs")
        try:
            smtp = db_retrieve_table_daemon(SMTP, entry='first')
            self.smtp_max_count = smtp.hourly_max
            self.smtp_wait_time = time.time() + 3600
            self.smtp_timer = time.time()
            self.email_count = 0
            self.allowed_to_send_notice = True

            outputs = db_retrieve_table_daemon(Output, entry='all')
            self.all_outputs_initialize(outputs)
            self.logger.debug("Outputs Initialized")
        except Exception as except_msg:
            self.logger.exception(
                "Problem initializing outputs: {err}".format(err=except_msg))

    def loop(self):
        """ Main loop of the output controller """
        if self.button_pressed:
            try:
                getattr(self.output[self.button_output_id], self.button_pressed)(self.button_args_dict)
            except:
                self.logger.exception("Error executing button press function '{}'".format(self.button_pressed))
            self.button_args_dict = None
            self.button_output_id = None
            self.button_pressed = None

        for output_id in self.output:
            for each_channel in self.output_unique_id[output_id]:
                # Execute if past the time the output was supposed to turn off
                if (self.output[output_id].output_setup and
                        self.output[output_id].output_on_until[each_channel] < datetime.datetime.now() and
                        self.output[output_id].output_on_duration[each_channel] and
                        not self.output[output_id].output_off_triggered[each_channel]):

                    # Use a thread to prevent blocking the loop
                    self.output[output_id].output_off_triggered[each_channel] = True
                    turn_output_off = threading.Thread(
                        target=self.output[output_id].output_on_off,
                        args=('off',),
                        kwargs={'output_channel': each_channel})
                    turn_output_off.start()

    def run_finally(self):
        """ Run when the controller is shutting down """
        # Turn all outputs to their shutdown state
        for each_output_id in self.output_unique_id:
            shutdown_timer = timeit.default_timer()
            # instruct each output to shutdown
            self.output[each_output_id].shutdown(shutdown_timer)

    def all_outputs_initialize(self, outputs):
        """ Initialize all output variables and classes """
        self.dict_outputs = parse_output_information()
        self.output_types = output_types()

        for each_output in outputs:
            try:
                self.output_type[each_output.unique_id] = each_output.output_type
                self.output_unique_id[each_output.unique_id] = {}

                if 'outputs_dict' in self.dict_outputs[each_output.output_type]:
                    for each_channel in self.dict_outputs[each_output.output_type]['outputs_dict']:
                        self.output_unique_id[each_output.unique_id][each_channel] = None
                else:
                    self.output_unique_id[each_output.unique_id][0] = None

                if each_output.output_type in self.dict_outputs:
                    output_loaded = load_module_from_file(
                        self.dict_outputs[each_output.output_type]['file_path'],
                        'outputs')
                    self.output[each_output.unique_id] = output_loaded.OutputModule(each_output)
                    self.output[each_output.unique_id].setup_output()
                    self.output[each_output.unique_id].init_post()

                self.logger.debug("{id} ({name}) Initialized".format(
                    id=each_output.unique_id.split('-')[0], name=each_output.name))
            except:
                self.logger.exception("Could not initialize output {}".format(
                    each_output.unique_id))

    def add_mod_output(self, output_id):
        """
        Add or modify local dictionary of output settings form SQL database

        When a output is added or modified while the output controller is
        running, these local variables need to also be modified to
        maintain consistency between the SQL database and running controller.

        :param output_id: Unique ID for each output
        :type output_id: str

        :return: 0 for success, 1 for fail, with success for fail message
        :rtype: int, str
        """
        try:
            self.dict_outputs = parse_output_information()

            output = db_retrieve_table_daemon(Output, unique_id=output_id)

            self.output_type[output_id] = output.output_type
            self.output_unique_id[output_id] = {}

            if 'outputs_dict' in self.dict_outputs[output.output_type]:
                for each_channel in self.dict_outputs[output.output_type]['outputs_dict']:
                    self.output_unique_id[output_id][each_channel] = None
            else:
                self.output_unique_id[output_id][0] = None

            if self.output_type[output_id] in self.dict_outputs:
                output_loaded = load_module_from_file(
                    self.dict_outputs[self.output_type[output_id]]['file_path'],
                    'outputs')
                self.output[output_id] = output_loaded.OutputModule(output)
                self.output[output_id].setup_output()
                self.output[output_id].init_post()

            return 0, "add_mod_output() Success"
        except Exception as e:
            return 1, "add_mod_output() Error: {id}: {e}".format(id=output_id, e=e)

    def del_output(self, output_id):
        """
        Delete output from being managed by Output controller

        :param output_id: Unique ID for output
        :type output_id: str

        :return: 0 for success, 1 for fail (with error message)
        :rtype: int, str
        """
        try:
            self.logger.debug("Output {id} Deleted.".format(id=output_id))

            # instruct output to shutdown
            shutdown_timer = timeit.default_timer()
            self.output[output_id].shutdown(shutdown_timer)

            self.output_unique_id.pop(output_id, None)
            self.output_type.pop(output_id, None)
            self.output.pop(output_id, None)

            return 0, "del_output() Success"
        except Exception as e:
            return 1, "del_output() Error: {id}: {e}".format(id=output_id, e=e)

    def output_on_off(self,
                      output_id,
                      state,
                      output_channel=0,
                      output_type=None,
                      amount=0.0,
                      min_off=0.0,
                      trigger_conditionals=True):
        """
        Manipulate an output by passing on/off, a volume, or a PWM duty cycle
        to the output module.

        :param output_id: ID for output
        :type output_id: str
        :param state: What state is desired? 'on', 1, True or 'off', 0, False
        :type state: str or int or bool
        :param output_channel: The output channel
        :type output_channel: int
        :param output_type: The type of output ('sec', 'vol', 'pwm')
        :type output_type: str
        :param amount: If state is 'on', an amount can be set (e.g. duration to stay on, volume to output, etc.)
        :type amount: float
        :param min_off: Don't allow on again for at least this amount (0 = disabled)
        :type min_off: float
        :param trigger_conditionals: Whether to allow trigger conditionals to act or not
        :type trigger_conditionals: bool
        """
        return self.output[output_id].output_on_off(
            state,
            output_channel=output_channel,
            output_type=output_type,
            amount=amount,
            min_off=min_off,
            trigger_conditionals=trigger_conditionals)

    def output_setup(self, action, output_id):
        """ Add, delete, or modify a specific output """
        if action in ['Add', 'Modify']:
            return self.add_mod_output(output_id)
        elif action == 'Delete':
            return self.del_output(output_id)
        else:
            return [1, 'Invalid output_setup action']

    def current_amp_load(self):
        """
        Calculate the sum of amps drawn from all outputs currently on

        :return: total Amperage draw
        :rtype: float
        """
        amp_load = 0.0
        for each_output_id, each_output_amps in self.output_amps.items():
            if self.is_on(each_output_id) and each_output_amps:
                amp_load += each_output_amps
        return amp_load

        # total_amps = 0
        # for output_id in self.output_unique_id:
        #     for each_channel in self.output_unique_id[output_id]:
        #         total_amps += self.output[output_id].current_amps(each_channel)
        # return total_amps

    def output_sec_currently_on(self, output_id, output_channel):
        return self.output[output_id].output_sec_currently_on(output_channel)

    def output_states_all(self):
        """
        Return a dictionary of all output states
        :rtype: dict
        """
        states = {}
        for output_id in self.output_unique_id:
            states[output_id] = {}
            for each_channel in self.output_unique_id[output_id]:
                states[output_id][each_channel] = self.output[output_id].output_state(each_channel)
        return states

    def is_on(self, output_id, output_channel=0):
        """
        CHeck if the output is on or off

        :param output_id: Unique ID for each output
        :type output_id: str

        :return: Whether the output is currently On (True) or Off (False)
        :rtype: bool
        """
        try:
            return self.output[output_id].is_on(output_channel=output_channel)
        except KeyError:
            self.logger.error("Output not found. This indicates the output controller either didn't properly "
                              "start or it experienced a fatal error.")
        except Exception:
            self.logger.exception("is_on() exception")

    def is_setup(self, output_id):
        """
        This function checks to see if the output is set up

        :param output_id: Unique ID for each output
        :type output_id: str

        :return: Is it safe to manipulate this output?
        :rtype: bool
        """
        try:
            return self.output[output_id].is_setup()
        except Exception:
            self.logger.exception("is_setup() exception")

    def custom_button_exec_function(self, output_id, button_id, args_dict):
        """Execute function from custom action button press"""
        self.button_args_dict = args_dict
        self.button_output_id = output_id
        self.button_pressed = button_id
        return 0, "Command sent to Output Controller"
