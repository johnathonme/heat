# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import datetime
from heat.common import exception
from heat.openstack.common import log as logging
from heat.openstack.common import timeutils
from heat.engine import timestamp
from heat.db import api as db_api
from heat.engine import parser
from heat.rpc import api as rpc_api

logger = logging.getLogger(__name__)


class WatchRule(object):
    WATCH_STATES = (
        ALARM,
        NORMAL,
        NODATA,
        SUSPENDED
    ) = (
        rpc_api.WATCH_STATE_ALARM,
        rpc_api.WATCH_STATE_OK,
        rpc_api.WATCH_STATE_NODATA,
        rpc_api.WATCH_STATE_SUSPENDED
    )
    ACTION_MAP = {ALARM: 'AlarmActions',
                  NORMAL: 'OKActions',
                  NODATA: 'InsufficientDataActions'}

    created_at = timestamp.Timestamp(db_api.watch_rule_get, 'created_at')
    updated_at = timestamp.Timestamp(db_api.watch_rule_get, 'updated_at')

    def __init__(self, context, watch_name, rule, stack_id=None,
                 state=NODATA, wid=None, watch_data=[],
                 last_evaluated=timeutils.utcnow()):
        self.context = context
        self.now = timeutils.utcnow()
        self.name = watch_name
        self.state = state
        self.rule = rule
        self.stack_id = stack_id
        self.timeperiod = datetime.timedelta(seconds=int(rule['Period']))
        self.id = wid
        self.watch_data = watch_data
        self.last_evaluated = last_evaluated

    @classmethod
    def load(cls, context, watch_name=None, watch=None):
        '''
        Load the watchrule object, either by name or via an existing DB object
        '''
        if watch is None:
            try:
                watch = db_api.watch_rule_get_by_name(context, watch_name)
            except Exception as ex:
                logger.warn('WatchRule.load (%s) db error %s' %
                            (watch_name, str(ex)))
        if watch is None:
            raise exception.WatchRuleNotFound(watch_name=watch_name)
        else:
            return cls(context=context,
                       watch_name=watch.name,
                       rule=watch.rule,
                       stack_id=watch.stack_id,
                       state=watch.state,
                       wid=watch.id,
                       watch_data=watch.watch_data,
                       last_evaluated=watch.last_evaluated)

    def store(self):
        '''
        Store the watchrule in the database and return its ID
        If self.id is set, we update the existing rule
        '''

        wr_values = {
            'name': self.name,
            'rule': self.rule,
            'state': self.state,
            'stack_id': self.stack_id
        }

        if not self.id:
            wr = db_api.watch_rule_create(self.context, wr_values)
            self.id = wr.id
        else:
            db_api.watch_rule_update(self.context, self.id, wr_values)

    def destroy(self):
        '''
        Delete the watchrule from the database.
        '''
        if self.id:
            db_api.watch_rule_delete(self.context, self.id)

    def do_data_cmp(self, data, threshold):
        op = self.rule['ComparisonOperator']
        if op == 'GreaterThanThreshold':
            return data > threshold
        elif op == 'GreaterThanOrEqualToThreshold':
            return data >= threshold
        elif op == 'LessThanThreshold':
            return data < threshold
        elif op == 'LessThanOrEqualToThreshold':
            return data <= threshold
        else:
            return False

    def do_Maximum(self):
        data = 0
        have_data = False
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            if not have_data:
                data = float(d.data[self.rule['MetricName']]['Value'])
                have_data = True
            if float(d.data[self.rule['MetricName']]['Value']) > data:
                data = float(d.data[self.rule['MetricName']]['Value'])

        if not have_data:
            return self.NODATA

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Minimum(self):
        data = 0
        have_data = False
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            if not have_data:
                data = float(d.data[self.rule['MetricName']]['Value'])
                have_data = True
            elif float(d.data[self.rule['MetricName']]['Value']) < data:
                data = float(d.data[self.rule['MetricName']]['Value'])

        if not have_data:
            return self.NODATA

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_SampleCount(self):
        '''
        count all samples within the specified period
        '''
        data = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            data = data + 1

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Average(self):
        data = 0
        samples = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            samples = samples + 1
            data = data + float(d.data[self.rule['MetricName']]['Value'])

        if samples == 0:
            return self.NODATA

        data = data / samples
        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Sum(self):
        data = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                logger.debug('ignoring %s' % str(d.data))
                continue
            data = data + float(d.data[self.rule['MetricName']]['Value'])

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def get_alarm_state(self):
        fn = getattr(self, 'do_%s' % self.rule['Statistic'])
        return fn()

    def evaluate(self):
        if self.state == self.SUSPENDED:
            return []
        # has enough time progressed to run the rule
        self.now = timeutils.utcnow()
        if self.now < (self.last_evaluated + self.timeperiod):
            return []
        return self.run_rule()

    def get_details(self):
        return {'alarm': self.name,
                'state': self.state}

    def run_rule(self):
        new_state = self.get_alarm_state()
        actions = self.rule_actions(new_state)
        self.state = new_state

        self.last_evaluated = self.now
        self.store()
        return actions

    def rule_actions(self, new_state):
        logger.info('WATCH: stack:%s, watch_name:%s %s',
                    self.stack_id, self.name, new_state)
        actions = []
        if self.ACTION_MAP[new_state] not in self.rule:
            logger.info('no action for new state %s',
                        new_state)
        else:
            s = db_api.stack_get(self.context, self.stack_id)
            stack = parser.Stack.load(self.context, stack=s)
            if (stack.action != stack.DELETE
                    and stack.status == stack.COMPLETE):
                for a in self.rule[self.ACTION_MAP[new_state]]:
                    actions.append(stack[a].signal)
            else:
                logger.warning("Could not process watch state %s for stack" %
                               new_state)
        return actions

    def create_watch_data(self, data):
        if self.state == self.SUSPENDED:
            logger.debug('Ignoring metric data for %s, SUSPENDED state'
                         % self.name)
            return []

        if self.rule['MetricName'] not in data:
            # Our simplified cloudwatch implementation only expects a single
            # Metric associated with each alarm, but some cfn-push-stats
            # options, e.g --haproxy try to push multiple metrics when we
            # actually only care about one (the one we're alarming on)
            # so just ignore any data which doesn't contain MetricName
            logger.debug('Ignoring metric data (only accept %s) : %s' %
                        (self.rule['MetricName'], data))
            return

        watch_data = {
            'data': data,
            'watch_rule_id': self.id
        }
        wd = db_api.watch_data_create(None, watch_data)
        logger.debug('new watch:%s data:%s' % (self.name, str(wd.data)))

    def state_set(self, state):
        '''
        Persistently store the watch state
        '''
        if state not in self.WATCH_STATES:
            raise ValueError("Invalid watch state %s" % state)

        self.state = state
        self.store()

    def set_watch_state(self, state):
        '''
        Temporarily set the watch state, returns list of functions to be
        scheduled in the stack ThreadGroup for the specified state
        '''

        if state not in self.WATCH_STATES:
            raise ValueError('Unknown watch state %s' % state)

        actions = []
        if state != self.state:
            actions = self.rule_actions(state)
            if actions:
                logger.debug("Overriding state %s for watch %s with %s" %
                            (self.state, self.name, state))
            else:
                logger.warning("Unable to override state %s for watch %s" %
                              (self.state, self.name))
        return actions
