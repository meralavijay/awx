# Copyright (c) 2016 Ansible, Inc.
# All Rights Reserved.

from copy import deepcopy
import datetime
import logging
import json

from django.db import models
from django.conf import settings
from django.core.mail.message import EmailMessage
from django.db import connection
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import smart_str, force_text
from jinja2 import sandbox
from jinja2.exceptions import TemplateSyntaxError, UndefinedError, SecurityError

# AWX
from awx.api.versioning import reverse
from awx.main.models.base import CommonModelNameNotUnique, CreatedModifiedModel, prevent_search
from awx.main.utils import encrypt_field, decrypt_field, set_environ
from awx.main.notifications.email_backend import CustomEmailBackend
from awx.main.notifications.slack_backend import SlackBackend
from awx.main.notifications.twilio_backend import TwilioBackend
from awx.main.notifications.pagerduty_backend import PagerDutyBackend
from awx.main.notifications.hipchat_backend import HipChatBackend
from awx.main.notifications.webhook_backend import WebhookBackend
from awx.main.notifications.mattermost_backend import MattermostBackend
from awx.main.notifications.grafana_backend import GrafanaBackend
from awx.main.notifications.rocketchat_backend import RocketChatBackend
from awx.main.notifications.irc_backend import IrcBackend
from awx.main.fields import JSONField


logger = logging.getLogger('awx.main.models.notifications')

__all__ = ['NotificationTemplate', 'Notification']


class NotificationTemplate(CommonModelNameNotUnique):

    NOTIFICATION_TYPES = [('email', _('Email'), CustomEmailBackend),
                          ('slack', _('Slack'), SlackBackend),
                          ('twilio', _('Twilio'), TwilioBackend),
                          ('pagerduty', _('Pagerduty'), PagerDutyBackend),
                          ('grafana', _('Grafana'), GrafanaBackend),
                          ('hipchat', _('HipChat'), HipChatBackend),
                          ('webhook', _('Webhook'), WebhookBackend),
                          ('mattermost', _('Mattermost'), MattermostBackend),
                          ('rocketchat', _('Rocket.Chat'), RocketChatBackend),
                          ('irc', _('IRC'), IrcBackend)]
    NOTIFICATION_TYPE_CHOICES = sorted([(x[0], x[1]) for x in NOTIFICATION_TYPES])
    CLASS_FOR_NOTIFICATION_TYPE = dict([(x[0], x[2]) for x in NOTIFICATION_TYPES])

    class Meta:
        app_label = 'main'
        unique_together = ('organization', 'name')
        ordering = ("name",)

    organization = models.ForeignKey(
        'Organization',
        blank=False,
        null=True,
        on_delete=models.CASCADE,
        related_name='notification_templates',
    )

    notification_type = models.CharField(
        max_length = 32,
        choices=NOTIFICATION_TYPE_CHOICES,
    )

    notification_configuration = prevent_search(JSONField(blank=False))

    def default_messages():
        return {'started': None, 'success': None, 'error': None}

    messages = JSONField(
        null=True,
        blank=True,
        default=default_messages,
        help_text=_('Optional custom messages for notification template.'))

    def has_message(self, condition):
        potential_template = self.messages.get(condition, {})
        if potential_template == {}:
            return False
        if potential_template.get('message', {}) == {}:
            return False
        return True

    def get_message(self, condition):
        return self.messages.get(condition, {})

    def build_notification_message(self, event_type, context):
        env = sandbox.ImmutableSandboxedEnvironment()
        templates = self.get_message(event_type)
        msg_template = templates.get('message', {})

        try:
            notification_subject = env.from_string(msg_template).render(**context)
        except (TemplateSyntaxError, UndefinedError, SecurityError):
            notification_subject = ''


        msg_body = templates.get('body', {})
        try:
            notification_body = env.from_string(msg_body).render(**context)
        except (TemplateSyntaxError, UndefinedError, SecurityError):
            notification_body = ''

        return (notification_subject, notification_body)

    def get_absolute_url(self, request=None):
        return reverse('api:notification_template_detail', kwargs={'pk': self.pk}, request=request)

    @property
    def notification_class(self):
        return self.CLASS_FOR_NOTIFICATION_TYPE[self.notification_type]

    def save(self, *args, **kwargs):
        new_instance = not bool(self.pk)
        update_fields = kwargs.get('update_fields', [])

        # preserve existing notification messages if not overwritten by new messages
        if not new_instance:
            old_nt = NotificationTemplate.objects.get(pk=self.id)
            old_messages = old_nt.messages
            new_messages = self.messages

            if old_messages is not None and new_messages is not None:
                for event in ['started', 'success', 'error']:
                    if not new_messages.get(event, {}) and old_messages.get(event, {}):
                        new_messages[event] = old_messages[event]
                        continue
                    if new_messages.get(event, {}) and old_messages.get(event, {}):
                        old_event_msgs = old_messages[event]
                        new_event_msgs = new_messages[event]
                        for msg_type in ['message', 'body']:
                            if msg_type not in new_event_msgs and old_event_msgs.get(msg_type, None):
                                new_event_msgs[msg_type] = old_event_msgs[msg_type]
                    new_messages.setdefault(event, None)

        for field in filter(lambda x: self.notification_class.init_parameters[x]['type'] == "password",
                            self.notification_class.init_parameters):
            if self.notification_configuration[field].startswith("$encrypted$"):
                continue
            if new_instance:
                value = self.notification_configuration[field]
                setattr(self, '_saved_{}_{}'.format("config", field), value)
                self.notification_configuration[field] = ''
            else:
                encrypted = encrypt_field(self, 'notification_configuration', subfield=field)
                self.notification_configuration[field] = encrypted
                if 'notification_configuration' not in update_fields:
                    update_fields.append('notification_configuration')
        super(NotificationTemplate, self).save(*args, **kwargs)
        if new_instance:
            update_fields = []
            for field in filter(lambda x: self.notification_class.init_parameters[x]['type'] == "password",
                                self.notification_class.init_parameters):
                saved_value = getattr(self, '_saved_{}_{}'.format("config", field), '')
                self.notification_configuration[field] = saved_value
                if 'notification_configuration' not in update_fields:
                    update_fields.append('notification_configuration')
            self.save(update_fields=update_fields)

    @property
    def recipients(self):
        return self.notification_configuration[self.notification_class.recipient_parameter]

    def generate_notification(self, subject, message):
        notification = Notification(notification_template=self,
                                    notification_type=self.notification_type,
                                    recipients=smart_str(self.recipients),
                                    subject=subject,
                                    body=message)
        notification.save()
        return notification

    def send(self, subject, body):
        for field in filter(lambda x: self.notification_class.init_parameters[x]['type'] == "password",
                            self.notification_class.init_parameters):
            if field in self.notification_configuration:
                self.notification_configuration[field] = decrypt_field(self,
                                                                       'notification_configuration',
                                                                       subfield=field)
        recipients = self.notification_configuration.pop(self.notification_class.recipient_parameter)
        if not isinstance(recipients, list):
            recipients = [recipients]
        sender = self.notification_configuration.pop(self.notification_class.sender_parameter, None)
        notification_configuration = deepcopy(self.notification_configuration)
        for field, params in self.notification_class.init_parameters.items():
            if field not in notification_configuration:
                if 'default' in params:
                    notification_configuration[field] = params['default']
        backend_obj = self.notification_class(**notification_configuration)
        notification_obj = EmailMessage(subject, backend_obj.format_body(body), sender, recipients)
        with set_environ(**settings.AWX_TASK_ENV):
            return backend_obj.send_messages([notification_obj])

    def display_notification_configuration(self):
        field_val = self.notification_configuration.copy()
        for field in self.notification_class.init_parameters:
            if field in field_val and force_text(field_val[field]).startswith('$encrypted$'):
                field_val[field] = '$encrypted$'
        return field_val


class Notification(CreatedModifiedModel):
    '''
    A notification event emitted when a NotificationTemplate is run
    '''

    NOTIFICATION_STATE_CHOICES = [
        ('pending', _('Pending')),
        ('successful', _('Successful')),
        ('failed', _('Failed')),
    ]

    class Meta:
        app_label = 'main'
        ordering = ('pk',)

    notification_template = models.ForeignKey(
        'NotificationTemplate',
        related_name='notifications',
        on_delete=models.CASCADE,
        editable=False
    )
    status = models.CharField(
        max_length=20,
        choices=NOTIFICATION_STATE_CHOICES,
        default='pending',
        editable=False,
    )
    error = models.TextField(
        blank=True,
        default='',
        editable=False,
    )
    notifications_sent = models.IntegerField(
        default=0,
        editable=False,
    )
    notification_type = models.CharField(
        max_length = 32,
        choices=NotificationTemplate.NOTIFICATION_TYPE_CHOICES,
    )
    recipients = models.TextField(
        blank=True,
        default='',
        editable=False,
    )
    subject = models.TextField(
        blank=True,
        default='',
        editable=False,
    )
    body = JSONField(blank=True)

    def get_absolute_url(self, request=None):
        return reverse('api:notification_detail', kwargs={'pk': self.pk}, request=request)


class JobNotificationMixin(object):
    STATUS_TO_TEMPLATE_TYPE = {'succeeded': 'success',
                               'running': 'started',
                               'failed': 'error'}
    # Tree of fields that can be safely referenced in a notification message
    JOB_FIELDS_WHITELIST = ['id', 'type', 'url', 'created', 'modified', 'name', 'description', 'job_type', 'playbook',
                            'forks', 'limit', 'verbosity', 'job_tags', 'force_handlers', 'skip_tags', 'start_at_task',
                            'timeout', 'use_fact_cache', 'launch_type', 'status', 'failed', 'started', 'finished',
                            'elapsed', 'job_explanation', 'execution_node', 'controller_node', 'allow_simultaneous',
                            'scm_revision', 'diff_mode', 'job_slice_number', 'job_slice_count', 'custom_virtualenv',
                            {'host_status_counts': ['skipped', 'ok', 'changed', 'failures', 'dark']},
                            {'playbook_counts': ['play_count', 'task_count']},
                            {'summary_fields': [{'inventory': ['id', 'name', 'description', 'has_active_failures',
                                                               'total_hosts', 'hosts_with_active_failures', 'total_groups',
                                                               'groups_with_active_failures', 'has_inventory_sources',
                                                               'total_inventory_sources', 'inventory_sources_with_failures',
                                                               'organization_id', 'kind']},
                                                {'project': ['id', 'name', 'description', 'status', 'scm_type']},
                                                {'project_update': ['id', 'name', 'description', 'status', 'failed']},
                                                {'job_template': ['id', 'name', 'description']},
                                                {'unified_job_template': ['id', 'name', 'description', 'unified_job_type']},
                                                {'instance_group': ['name', 'id']},
                                                {'created_by': ['id', 'username', 'first_name', 'last_name']},
                                                {'labels': ['count', 'results']},
                                                {'source_workflow_job': ['description', 'elapsed', 'failed', 'id', 'name', 'status']}]}]

    @classmethod
    def context_stub(cls):
        """Returns a stub context that can be used for validating notification messages.
        Context has the same structure as the context that will actually be used to render
        a notification message."""
        context = {'job': {'allow_simultaneous': False,
                           'controller_node': 'foo_controller',
                           'created': datetime.datetime(2018, 11, 13, 6, 4, 0, 0, tzinfo=datetime.timezone.utc),
                           'custom_virtualenv': 'my_venv',
                           'description': 'Sample job description',
                           'diff_mode': False,
                           'elapsed': 0.403018,
                           'execution_node': 'awx',
                           'failed': False,
                           'finished': False,
                           'force_handlers': False,
                           'forks': 0,
                           'host_status_counts': {'skipped': 1, 'ok': 5, 'changed': 3, 'failures': 0, 'dark': 0},
                           'id': 42,
                           'job_explanation': 'Sample job explanation',
                           'job_slice_count': 1,
                           'job_slice_number': 0,
                           'job_tags': '',
                           'job_type': 'run',
                           'launch_type': 'workflow',
                           'limit': 'bar_limit',
                           'modified': datetime.datetime(2018, 12, 13, 6, 4, 0, 0, tzinfo=datetime.timezone.utc),
                           'name': 'Stub JobTemplate',
                           'playbook_counts': {'play_count': 5, 'task_count': 10},
                           'playbook': 'ping.yml',
                           'scm_revision': '',
                           'skip_tags': '',
                           'start_at_task': '',
                           'started': '2019-07-29T17:38:14.137461Z',
                           'status': 'running',
                           'summary_fields': {'created_by': {'first_name': '',
                                                             'id': 1,
                                                             'last_name': '',
                                                             'username': 'admin'},
                                              'instance_group': {'id': 1, 'name': 'tower'},
                                              'inventory': {'description': 'Sample inventory description',
                                                            'groups_with_active_failures': 0,
                                                            'has_active_failures': False,
                                                            'has_inventory_sources': False,
                                                            'hosts_with_active_failures': 0,
                                                            'id': 17,
                                                            'inventory_sources_with_failures': 0,
                                                            'kind': '',
                                                            'name': 'Stub Inventory',
                                                            'organization_id': 121,
                                                            'total_groups': 0,
                                                            'total_hosts': 1,
                                                            'total_inventory_sources': 0},
                                              'job_template': {'description': 'Sample job template description',
                                                               'id': 39,
                                                               'name': 'Stub JobTemplate'},
                                              'labels': {'count': 0, 'results': []},
                                              'project': {'description': 'Sample project description',
                                                          'id': 38,
                                                          'name': 'Stub project',
                                                          'scm_type': 'git',
                                                          'status': 'successful'},
                                              'project_update': {'id': 5, 'name': 'Stub Project Update', 'description': 'Project Update',
                                                                 'status': 'running', 'failed': False},
                                              'unified_job_template': {'description': 'Sample unified job template description',
                                                                       'id': 39,
                                                                       'name': 'Stub Job Template',
                                                                       'unified_job_type': 'job'},
                                              'source_workflow_job': {'description': 'Sample workflow job description',
                                                                      'elapsed': 0.000,
                                                                      'failed': False,
                                                                      'id': 88,
                                                                      'name': 'Stub WorkflowJobTemplate',
                                                                      'status': 'running'}},
                           'timeout': 0,
                           'type': 'job',
                           'url': '/api/v2/jobs/13/',
                           'use_fact_cache': False,
                           'verbosity': 0},
                   'job_friendly_name': 'Job',
                   'url': 'https://towerhost/#/jobs/playbook/1010',
                   'job_summary_dict': """{'url': 'https://towerhost/$/jobs/playbook/13',
 'traceback': '',
 'status': 'running',
 'started': '2019-08-07T21:46:38.362630+00:00',
 'project': 'Stub project',
 'playbook': 'ping.yml',
 'name': 'Stub Job Template',
 'limit': '',
 'inventory': 'Stub Inventory',
 'id': 42,
 'hosts': {},
 'friendly_name': 'Job',
 'finished': False,
 'credential': 'Stub credential',
 'created_by': 'admin'}"""}

        return context

    def context(self, serialized_job):
        """Returns a context that can be used for rendering notification messages.
        Context contains whitelisted content retrieved from a serialized job object
        (see JobNotificationMixin.JOB_FIELDS_WHITELIST), the job's friendly name,
        and a url to the job run."""
        context = {'job': {},
                   'job_friendly_name': self.get_notification_friendly_name(),
                   'url': self.get_ui_url(),
                   'job_summary_dict': json.dumps(self.notification_data(), indent=4)}

        def build_context(node, fields, whitelisted_fields):
            for safe_field in whitelisted_fields:
                if type(safe_field) is dict:
                    field, whitelist_subnode = safe_field.copy().popitem()
                    # ensure content present in job serialization
                    if field not in fields:
                        continue
                    subnode = fields[field]
                    node[field] = {}
                    build_context(node[field], subnode, whitelist_subnode)
                else:
                    # ensure content present in job serialization
                    if safe_field not in fields:
                        continue
                    node[safe_field] = fields[safe_field]
        build_context(context['job'], serialized_job, self.JOB_FIELDS_WHITELIST)

        return context

    def get_notification_templates(self):
        raise RuntimeError("Define me")

    def get_notification_friendly_name(self):
        raise RuntimeError("Define me")

    def notification_data(self):
        raise RuntimeError("Define me")

    def build_notification_message(self, nt, status):
        env = sandbox.ImmutableSandboxedEnvironment()

        from awx.api.serializers import UnifiedJobSerializer
        job_serialization = UnifiedJobSerializer(self).to_representation(self)
        context = self.context(job_serialization)

        msg_template = body_template = None

        if nt.messages:
            templates = nt.messages.get(self.STATUS_TO_TEMPLATE_TYPE[status], {}) or {}
            msg_template = templates.get('message', {})
            body_template = templates.get('body', {})

        if msg_template:
            try:
                notification_subject = env.from_string(msg_template).render(**context)
            except (TemplateSyntaxError, UndefinedError, SecurityError):
                notification_subject = ''
        else:
            notification_subject = u"{} #{} '{}' {}: {}".format(self.get_notification_friendly_name(),
                                                                self.id,
                                                                self.name,
                                                                status,
                                                                self.get_ui_url())
        notification_body = self.notification_data()
        notification_body['friendly_name'] = self.get_notification_friendly_name()
        if body_template:
            try:
                notification_body['body'] = env.from_string(body_template).render(**context)
            except (TemplateSyntaxError, UndefinedError, SecurityError):
                notification_body['body'] = ''

        return (notification_subject, notification_body)

    def send_notification_templates(self, status):
        from awx.main.tasks import send_notifications  # avoid circular import
        if status not in ['running', 'succeeded', 'failed']:
            raise ValueError(_("status must be either running, succeeded or failed"))
        try:
            notification_templates = self.get_notification_templates()
        except Exception:
            logger.warn("No notification template defined for emitting notification")
            return

        if not notification_templates:
            return

        for nt in set(notification_templates.get(self.STATUS_TO_TEMPLATE_TYPE[status], [])):
            try:
                (notification_subject, notification_body) = self.build_notification_message(nt, status)
            except AttributeError:
                raise NotImplementedError("build_notification_message() does not exist" % status)

            # Use kwargs to force late-binding
            # https://stackoverflow.com/a/3431699/10669572
            def send_it(local_nt=nt, local_subject=notification_subject, local_body=notification_body):
                def _func():
                    send_notifications.delay([local_nt.generate_notification(local_subject, local_body).id],
                                             job_id=self.id)
                return _func
            connection.on_commit(send_it())
