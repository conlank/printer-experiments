#!/usr/bin/env python3
# Script to run a Klipper command multiple times, capture related values, and show stats.
# To see arguments, invoke this script with:
#   ./run_test.py -h

import argparse
import json
import os
import pprint
import statistics
import random
import sys
import time

import requests

# Moonraker API
# https://moonraker.readthedocs.io/en/latest/web_api/#json-rpc-api-overview
# https://github.com/Arksine/moonraker/blob/master/docs/web_api.md
# What we're using:
# https://github.com/Arksine/moonraker/blob/master/docs/web_api.md#request-cached-gcode-responses

# Set this high enough to handle any command you'd run.
# Note, however, that Moonraker by default throws a 200 after exactly
# one minute, even with this value set to exceed one minute.
READ_TIMEOUT=180

# Common-arg defaults:
DEFAULT_COMMAND = "probe_accuracy"
DEFAULT_ITERATIONS = 1
DEFAULT_OUTPUT_PATH = "results.json"

# Text used to indicate in the console the beginning of this script's execution.
MARKER_MESSAGE_GCODE = "M117 Running Test"

# Range in which to select random motions for each test.
Z_TILT_ADJUST_MOVED_RANDOMIZED_RANGE = (2, 7)

# Gap between tests; tries to avoid an apparent race condition where a just-written
# log entry (created by an M117 message) is not made visible to the next call to
# get cached responses.
AFTER_MARKER_GAP = 1.0

# Change these to match your printer, for sure.
START_GCODES = ["G28 X", "T0", "G28", "ATTACH_PROBE"]
END_GCODES = ["DETACH_PROBE"]

# Extents of jittering when running Z_TILT measurements
DEFAULT_Z_TILT_RANDOM_MOVE_MIN = 2
DEFAULT_Z_TILT_RANDOM_MOVE_MAX = 7


def PROCESSING_FCN_PROBE_ACCURACY(messages, verbose):
    # Sample message:
    # {'message': '// probe accuracy results: maximum 11.995491, '
    #             'minimum 11.992991, range 0.002500, average '
    #             '11.994658, median 11.995491, standard deviation '
    #             '0.001179',
    def extract_range(input):
        return float(input.split(',')[2].split('range ')[1])

    probe_messages = [m for m in messages if "probe accuracy results" in m["message"]]
    if verbose:
        print("Probe messages:")
        pprint.pprint(probe_messages)

    values = [extract_range(m["message"]) for m in probe_messages]
    return statistics.median(values)


def PROCESSING_FCN_Z_TILT_ADJUST(messages, verbose):
    # Sample message:
    # {'message': '// Retries: 0/3 Probed points range: 0.005000 '
    #             'tolerance: 0.010000',
    #  'time': 1645515774.3865793,
    #  'type': 'response'},
    def extract_retries(input):
        return int(input.split('Retries:')[1].split('/')[0].strip()[0])

    probe_messages = [m for m in messages if "Retries" in m["message"]]
    if verbose:
        print("Probe messages:")
        pprint.pprint(probe_messages)

    # Get a list of retries.  The last-seen retry message indicates the actual
    # number of retries.
    retries = [extract_retries(m["message"]) for m in probe_messages]
    num_retries = retries[-1]
    return num_retries


def COMMANDS_FCN_Z_TILT_ADJUST_MOVED(args):
    return [
        "FORCE_MOVE STEPPER=stepper_z DISTANCE=2 VELOCITY=40",
        "Z_TILT_ADJUST",
    ]


def COMMANDS_FCN_Z_TILT_ADJUST_MOVED_RANDOMIZED(args):
    dist = random.uniform(args.z_tilt_random_move_min, \
        args.z_tilt_random_move_max)
    print("Using random distance: %0.3f" % dist)
    return [
        "FORCE_MOVE STEPPER=stepper_z DISTANCE=%0.3f VELOCITY=40" % dist,
        "Z_TILT_ADJUST",
    ]


# Test data and functions.
# Values:
#  'commands_fcn': parameter-less fcn to return a list of commands to run
#  'messages_per_command': # of min messages to read per command
#  'processing_fcn':
#      - inputs: a list of messages, verbose
#      - returns: a singular value
# TODO: use proper classes here
COMMANDS = {
    # PROBE_ACCURACY test with a few samples.
    'probe_accuracy': {
        'commands_fcn': lambda args: ["PROBE_ACCURACY samples=3"],
        'messages_per_command': 10,
        'processing_fcn': PROCESSING_FCN_PROBE_ACCURACY,
    },
    # Z_TILT_ADJUST test with no intentional out-of-flat change in between.
    'z_tilt_adjust_no_reset': {
        'commands_fcn': lambda args: ["Z_TILT_ADJUST"],
        # 3 to 5 probes per location; if increasing to 5, then there's an extra message.
        # Up to 4 retries.
        # So: 4 * (6 * 4) --> 100+ messages.
        'messages_per_command': 75,
        'processing_fcn': PROCESSING_FCN_Z_TILT_ADJUST,
    },
    # Z_TILT_ADJUST test where the Z tilt is intentionally messed up after each iteration.
    # The distance for motion is always the same.
    'z_tilt_adjust_moved': {
        'commands_fcn': COMMANDS_FCN_Z_TILT_ADJUST_MOVED,
        # Same as above, plus others for our commands.
        'messages_per_command': 150,
        'processing_fcn': PROCESSING_FCN_Z_TILT_ADJUST,
    },
    # Z_TILT_ADJUST test where the Z tilt is intentionally messed up after each iteration.
    # The distance for motion is randomized within a range.
    'z_tilt_adjust_moved_randomized': {
        'commands_fcn': COMMANDS_FCN_Z_TILT_ADJUST_MOVED_RANDOMIZED,
        # Same as above, plus others for our commands.
        'messages_per_command': 200,
        'processing_fcn': PROCESSING_FCN_Z_TILT_ADJUST,
    },
}


def run_gcode(printer, gcode, verbose=False):
    """Run a gcode command to completion and return the result.
    https://github.com/Arksine/moonraker/blob/master/docs/web_api.md#run-a-gcode
    """
    r = requests.post("http://" + printer + "/printer/gcode/script?script=" + gcode, timeout=(1, READ_TIMEOUT))
    if verbose:
        print(r.status_code)
    # Disabled for now to workaround 60-second presumably-Moonraker timeout
    #assert(r.status_code == 200)
    return r

def get_cached_gcode(printer, count, verbose=False):
    """Get cached gcode, up to the amount specified.
    https://moonraker.readthedocs.io/en/latest/web_api/#http-api-overview
    GET /server/gcode_store?count=100
    """
    r = requests.get("http://" + printer + ("/server/gcode_store?count=%s" % count), timeout=(1, READ_TIMEOUT))
    if verbose:
        print(r.status_code)
    # Disabled for now to workaround 60-second presumably-Moonraker timeout
    #assert(r.status_code == 200)
    return r


class KlipperTest:

    def __init__(self, printer, verbose, iterations, commands_fcn, processing_fcn, messages_per_command, args):
        self.printer = printer
        self.verbose = verbose
        self.iterations = iterations
        self.commands_fcn = commands_fcn
        self.processing_fcn = processing_fcn
        self.messages_per_command = messages_per_command
        self.args = args

        self.marker_message = None
        self.results = None  # List of results value (single floats)

    def _get_marker_message(self):
        """Send a dummy message, which can be used in future log-scraping.

        Returns the marker message.
        """
        # Get time from the printer, to use with rejecting earlier cached gcode
        run_gcode(self.printer, MARKER_MESSAGE_GCODE)

        # Hacky wait time.
        time.sleep(AFTER_MARKER_GAP)

        result = get_cached_gcode(self.printer, 2)
        result_json = result.json()["result"]
        if self.verbose:
            pprint.pprint(result_json)

        # Messages look like this:
        # {'gcode_store': [{'message': 'M117 Hello',
        #                  'time': 1645515805.776437,
        #                  'type': 'command'}]}
        entry = result_json["gcode_store"][-1]
        assert entry["message"] == MARKER_MESSAGE_GCODE, "Expected %s but got %s, from %s" % (MARKER_MESSAGE_GCODE, entry["message"], result_json)
        assert entry["type"] == "command"
        return entry

    def run(self):
        for gcode in START_GCODES:
            run_gcode(self.printer, gcode)
        self.results = []
        for i in range(self.iterations):
            # Attempt to clear the cache somehow.
            result = get_cached_gcode(self.printer, self.messages_per_command)

            # Then, send our message.
            self.marker_message = self._get_marker_message()
            commands = self.commands_fcn(self.args)
            for command in commands:
                run_gcode(self.printer, command)

            result = get_cached_gcode(self.printer, self.messages_per_command)
            result_json = result.json()["result"]
            if self.verbose:
                pprint.pprint(result_json)

            messages = result_json["gcode_store"]

            # Ensure that we collected enough messages to see the original, first message.
            if self.marker_message not in messages:
                print("Unable to find original message in collected list; increase messages_per_command"
                        "from %s and try again." % self.messages_per_command)
                sys.exit(1)

            message_index = messages.index(self.marker_message)
            if self.verbose:
                print("Found original message at index %s" % message_index)
            messages_filtered = result_json["gcode_store"][message_index + 1:]
            if self.verbose:
                print("Filtered messages:")
                pprint.pprint(messages_filtered)

            result = self.processing_fcn(messages_filtered, self.verbose)
            print("> Result: %s" % result)
            self.results.append(result)

        for gcode in END_GCODES:
            run_gcode(self.printer, gcode)


    def get_results(self):
        return self.results


def run_test(args):
    start_time = time.time()
    printer = args.printer
    test_type = args.test_type
    iterations = int(args.iterations)
    command_metadata = COMMANDS[test_type]
    commands_fcn = command_metadata["commands_fcn"]
    processing_fcn = command_metadata["processing_fcn"]
    messages_per_command = command_metadata["messages_per_command"]

    test = KlipperTest(args.printer, args.verbose, iterations, commands_fcn, processing_fcn, messages_per_command, args)
    print("Starting test.")
    test.run()
    print("Test completed.")
    print("Ran %i iterations." % iterations)
    data = test.get_results()
    print("Data: %s" % data)

    if args.stats:
        print("Printing stats:")
        median = statistics.median(data)
        print("  Min: %0.4f" % min(data))
        print("  Max: %0.4f" % max(data))
        print("  Median: %0.4f" % median)
        if len(data) > 1:
            s = statistics.stdev(data)
            print("  Standard Deviation: %0.3f" % s)

    total_time = time.time() - start_time
    time_per_iteration = total_time / iterations
    print("--- %0.2f seconds total; %0.2f per iteration ---" % (total_time, time_per_iteration))

    if args.output:
        with(open(args.output_path, 'w') as outfile):
            json.dump(data, outfile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an automated, multi-iteration Klipper test.")
    parser.add_argument('printer', help="Printer address, whether IP or zeroconf - something like mainsailos.local")
    parser.add_argument('--test_type', help="Test type", choices=COMMANDS.keys(), default=DEFAULT_COMMAND)
    parser.add_argument('--verbose', help="Use more-verbose debug output", action='store_true')
    parser.add_argument('--iterations', help="Number of test iterations", default=DEFAULT_ITERATIONS)
    parser.add_argument('--stats', help="Show stats", action='store_true')
    parser.add_argument('--output', help="Write output data?", action='store_true')
    parser.add_argument('--output_path', help="Directory at which to write output data", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument('--z_tilt_random_move_min', help="When jittering Z_TILT test, minimum of range to move", default=DEFAULT_Z_TILT_RANDOM_MOVE_MIN)
    parser.add_argument('--z_tilt_random_move_max', help="When jittering Z_TILT test, maximum of range to move", default=DEFAULT_Z_TILT_RANDOM_MOVE_MAX)
    args = parser.parse_args()

    run_test(args)
