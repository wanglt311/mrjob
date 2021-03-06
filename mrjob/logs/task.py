# -*- coding: utf-8 -*-
# Copyright 2015-2016 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Parse "task" logs, which are the syslog and stderr for each individual
task and typically appear in the userlogs/ directory."""
import posixpath
import re

from mrjob.util import file_ext
from .ids import _add_implied_task_id
from .ids import _to_job_id
from .log4j import _parse_hadoop_log4j_records
from .wrap import _cat_log
from .wrap import _ls_logs


# Match a java exception, possibly preceded by 'PipeMapRed failed!', etc.
# use this with search()
_JAVA_TRACEBACK_RE = re.compile(
    r'$\s+at .*\((.*\.java:\d+|Native Method)\)$',
    re.MULTILINE)

# this seems to only happen for S3. Not sure if this happens in YARN
_OPENING_FOR_READING_RE = re.compile(
    r"^Opening '(?P<path>.*?)' for reading$")

# what log paths look like pre-YARN
_PRE_YARN_TASK_LOG_PATH_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<attempt_id>attempt_(?P<timestamp>\d+)_(?P<step_num>\d+)_'
    r'(?P<task_type>[mr])_(?P<task_num>\d+)_'
    r'(?P<attempt_num>\d+))/'
    r'(?P<log_type>stderr|syslog)(?P<suffix>\.\w{1,3})?$')
# TODO: add stdout log type for Spark


# ignore warnings about initializing log4j in task stderr
_TASK_STDERR_IGNORE_RE = re.compile(r'^log4j:WARN .*$')

# this is the start of a Java stacktrace that Hadoop 1 always logs to
# stderr when tasks fail (see #1430)
_SUBPROCESS_FAILED_STACK_TRACE_START = re.compile(
    r'^java\.lang\.RuntimeException: PipeMapRed\.waitOutputThreads\(\):'
    r' subprocess failed with code .*$')

# message telling us about a (input) split. Looks like this:
#
# Processing split: hdfs://ddf64167693a:9000/path/to/bootstrap.sh:0+335
_YARN_INPUT_SPLIT_RE = re.compile(
    r'^Processing split:\s+(?P<path>.*)'
    r':(?P<start_line>\d+)\+(?P<num_lines>\d+)$')

# what log paths look like on YARN
_YARN_TASK_LOG_PATH_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<application_id>application_\d+_\d{4})/'
    r'(?P<container_id>container(_\d+)+)/'
    r'(?P<log_type>stderr|syslog)(?P<suffix>\.\w{1,3})?$')
# TODO: add stdout log type for Spark


def _ls_task_logs(fs, log_dir_stream, application_id=None, job_id=None):
    """Yield matching logs, optionally filtering by application_id
    or job_id.

    This will yield matches for stderr logs first, followed by syslogs. stderr
    logs will have a 'syslog' field pointing to the match for the
    corresponding syslog (stderr logs without a corresponding syslog won't be
    included).
    """
    stderr_logs = []
    syslogs = []

    for match in _ls_logs(fs, log_dir_stream, _match_task_log_path,
                          application_id=application_id,
                          job_id=job_id):
        if match['log_type'] == 'stderr':
            stderr_logs.append(match)
        elif match['log_type'] == 'syslog':
            syslogs.append(match)

    # use to match stderr logs to corresponding syslogs
    def _log_key(match):
        return tuple((k, v) for k, v in sorted(match.items())
                     if k not in ('log_type', 'path'))

    key_to_syslog = dict((_log_key(match), match) for match in syslogs)

    for stderr_log in stderr_logs:
        stderr_log['syslog'] = key_to_syslog.get(_log_key(stderr_log))

    # exclude stderr logs with no syslog
    stderr_logs_with_syslog = [m for m in stderr_logs if m['syslog']]

    return stderr_logs_with_syslog + syslogs


def _match_task_log_path(path, application_id=None, job_id=None):
    """Is this the path/URI of a task log?

    If so, return a dictionary containing application_id and container_id
    (on YARN) or attempt_id (on pre-YARN Hadoop), plus log_type (either
    stdout, stderr, or syslog).

    Otherwise, return None

    Optionally, filter by application_id (YARN) or job_id (pre-YARN).
    """
    m = _PRE_YARN_TASK_LOG_PATH_RE.match(path)
    if m:
        if job_id and job_id != _to_job_id(m.group('attempt_id')):
            return None  # matches, but wrong job_id
        return dict(
            attempt_id=m.group('attempt_id'),
            log_type=m.group('log_type'))

    m = _YARN_TASK_LOG_PATH_RE.match(path)
    if m:
        if application_id and application_id != m.group('application_id'):
            return None  # matches, but wrong application_id
        return dict(
            application_id=m.group('application_id'),
            container_id=m.group('container_id'),
            log_type=m.group('log_type'))

    return None


def _interpret_task_logs(fs, matches, partial=True, log_callback=None):
    """Look for errors in task syslog/stderr.

    If *partial* is true (the default), stop when we find the first error
    that includes a *task_error*.

    If *log_callback* is set, every time we're about to parse a
        file, call it with a single argument, the path of that file

    Returns a dictionary possibly containing the key 'errors', which
    is a dict containing:

    hadoop_error:
        message: string containing error message and Java exception
        num_lines: number of lines in syslog this takes up
        path: syslog we read this error from
        start_line: where in syslog exception starts (0-indexed)
    split: (optional)
        path: URI of input file task was processing
        num_lines: (optional) number of lines in split
        start_line: (optional) first line of split (0-indexed)
    task_error:
        message: command and error message from task, as a string
        num_lines: number of lines in stderr this takes up
        path: stderr we read this from
        start_line: where in stderr error message starts (0-indexed)

    In addition, if *partial* is set to true (and we found an error),
    this dictionary will contain the key *partial*, set to True.
    """
    result = {}
    syslogs_parsed = set()

    for match in matches:
        error = {}

        # are is this match for a stderr file, or a syslog?
        if match.get('syslog'):
            stderr_path = match['path']
            syslog_path = match['syslog']['path']
        else:
            stderr_path = None
            syslog_path = match['path']

        if stderr_path:
            if log_callback:
                log_callback(stderr_path)
            task_error = _parse_task_stderr(_cat_log(fs, stderr_path))

            if task_error:
                task_error['path'] = stderr_path
                error['task_error'] = task_error
            else:
                continue  # can parse syslog independently later

        # already parsed this syslog in conjunction with an earlier task error
        if syslog_path in syslogs_parsed:
            continue

        if log_callback:
            log_callback(syslog_path)
        syslog_error = _parse_task_syslog(_cat_log(fs, syslog_path))
        syslogs_parsed.add(syslog_path)

        if not syslog_error.get('hadoop_error'):
            # if no entry in Hadoop syslog, probably just noise
            continue

        error.update(syslog_error)
        error['hadoop_error']['path'] = syslog_path

        # path in IDs we learned from path
        for id_key in 'attempt_id', 'container_id':
            if id_key in match:
                error[id_key] = match[id_key]
        _add_implied_task_id(error)

        result.setdefault('errors', [])
        result['errors'].append(error)

        if partial:
            result['partial'] = True
            break

    return result


def _parse_task_syslog(lines):
    """Parse an error out of a syslog file.

    Returns a dict, possibly containing the following keys:

    hadoop_error:
        message: string containing error message and Java exception
        num_lines: number of lines in syslog this takes up
        start_line: where in syslog exception starts (0-indexed)
    split: (optional)
        path: URI of input file task was processing
        num_lines: (optional) number of lines in split
        start_line: (optional) first line of split (0-indexed)
    """
    result = {}

    for record in _parse_hadoop_log4j_records(lines):
        message = record['message']

        m = _OPENING_FOR_READING_RE.match(message)
        if m:
            result['split'] = dict(path=m.group('path'))
            continue

        m = _YARN_INPUT_SPLIT_RE.match(message)
        if m:
            result['split'] = dict(
                path=m.group('path'),
                start_line=int(m.group('start_line')),
                num_lines=int(m.group('num_lines')))
            continue

        m = _JAVA_TRACEBACK_RE.search(message)
        if m:
            result['hadoop_error'] = dict(
                message=message,
                num_lines=record['num_lines'],
                start_line=record['start_line'],
            )
            break  # nothing to do once we've found the error

    return result


def _parse_task_stderr(lines):
    """Attempt to explain any error in task stderr, be it a Python
    exception or a problem with a setup command (see #1203).

    Looks for '+ ' followed by a command line, and then the command's
    stderr. If there are no such lines (because we're not using a setup
    script), assumes the entire file contents are the cause of error.

    Returns a task error dictionary with the following keys, or None
    if the file is empty.

    message: a string (e.g. Python command line followed by Python traceback)
    start_line: where in lines message appears (0-indexed)
    num_lines: how may lines the message takes up
    """
    task_error = None
    stack_trace_start_line = None

    for line_num, line in enumerate(lines):
        line = line.rstrip('\r\n')

        # ignore "subprocess failed" stack trace
        if _SUBPROCESS_FAILED_STACK_TRACE_START.match(line):
            stack_trace_start_line = line_num
            continue

        # once we detect a stack trace, keep ignoring lines until
        # we find a non-indented one
        if stack_trace_start_line is not None:
            if line.lstrip() != line:
                continue
            else:
                stack_trace_start_line = None

        # ignore warnings about initializing log4j
        if _TASK_STDERR_IGNORE_RE.match(line):
            # ignored lines shouldn't count as part of the line range
            if task_error and 'num_lines' not in task_error:
                task_error['num_lines'] = line_num - task_error['start_line']
            continue
        elif not task_error or line.startswith('+ '):
            task_error = dict(
                message=line,
                start_line=line_num)
        else:
            task_error['message'] += '\n' + line

    if task_error:
        if 'num_lines' not in task_error:
            end_line = stack_trace_start_line or (line_num + 1)
            task_error['num_lines'] = end_line - task_error['start_line']
        return task_error
    else:
        return None
