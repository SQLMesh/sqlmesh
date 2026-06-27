# Notifications guide

SQLMesh can send notifications via Slack or email when certain events occur. This page describes how to configure notifications and specify recipients.

## Notification targets

Notifications are configured with `notification targets`. Targets are specified in a project's [configuration](https://sqlmesh.readthedocs.io/en/stable/reference/configuration/) file (`config.yml` or `config.py`), and multiple targets can be specified for a project.

A project may specify both global and user-specific notifications. Each target's notifications will be sent for all instances of each [event type](#sqlmesh-event-types) (e.g., notifications for `run` will be sent for *all* of the project's environments), with exceptions for audit failures and when an [override is configured for development](#notifications-during-development).

[Audit](../concepts/audits.md) failure notifications can be sent for specific models if five conditions are met:

1. A model's `owner` field is populated
2. The model executes one or more audits
3. The owner has a user-specific notification target configured
4. The owner's notification target `notify_on` key includes audit failure events
5. The audit fails in the `prod` environment

When those conditions are met, the audit owner will be notified if their audit failed in the `prod` environment.

There are three types of notification target, corresponding to the two [Slack notification methods](#slack-notifications) and [email notification](#email-notifications). They are specified in either a specific user's `notification_targets` key or the top-level `notification_targets` configuration key.

This example shows the location of both user-specific and global notification targets:

=== "YAML"

    ```yaml linenums="1"
    # User notification targets
    users:
      - username: User1
        ...
        notification_targets:
          - notification_target_1
            ...
          - notification_target_2
            ...
      - username: User2
        ...
        notification_targets:
          - notification_target_1
            ...
          - notification_target_2
            ...

    # Global notification targets
    notification_targets:
      - notification_target_1
        ...
      - notification_target_2
        ...
    ```

=== "Python"

    ```python linenums="1"
    config = Config(
        ...,
        # User notification targets
        users=[
            User(
                username="User1",
                notification_targets=[
                    notification_target_1(...),
                    notification_target_2(...),
                ],
            ),
            User(
                username="User2",
                notification_targets=[
                    notification_target_1(...),
                    notification_target_2(...),
                ],
            )
        ],

        # Global notification targets
        notification_targets=[
            notification_target_1(...),
            notification_target_2(...),
        ],
        ...
    )
    ```

### Notifications During Development

Events triggering notifications may be executed repeatedly during code development. To prevent excessive notification, SQLMesh can stop all but one user's notification targets.

Specify the top-level `username` configuration key with a value also present in a user-specific notification target's `username` key to only notify that user. This key can be specified in either the project configuration file or a machine-specific configuration file located in `~/.sqlmesh`. The latter may be useful if a specific machine is always used for development.

This example stops all notifications other than those for `User1`:

=== "YAML"

    ```yaml linenums="1" hl_lines="1-2"
    # Top-level `username` key: only notify User1
    username: User1
    # User1 notification targets
    users:
      - username: User1
        ...
        notification_targets:
          - notification_target_1
            ...
          - notification_target_2
            ...
    ```

=== "Python"

    ```python linenums="1" hl_lines="3-4"
    config = Config(
        ...,
        # Top-level `username` key: only notify User1
        username="User1",
        users=[
            User(
                # User1 notification targets
                username="User1",
                notification_targets=[
                    notification_target_1(...),
                    notification_target_2(...),
                ],
            ),
        ]
    )
    ```

## SQLMesh Event Types

SQLMesh notifications are triggered by events. The events that should trigger a notification are specified in the notification target's `notify_on` field.

Notifications are supported for [`plan` application](../concepts/plans.md) start/end/failure, [`run`](../reference/cli.md#run) start/end/failure, and [`audit`](../concepts/audits.md) failures.

For `plan` and `run` start/end, the target environment name is included in the notification message. For failures, the Python exception or error text is included in the notification message.

This table lists each event, its associated `notify_on` value, and its notification message:

| Event                         | `notify_on` Key Value  | Notification message                                     |
| ----------------------------- | ---------------------- | -------------------------------------------------------- |
| Plan application start        | apply_start            | "Plan apply started for environment `{environment}`."    |
| Plan application end          | apply_end              | "Plan apply finished for environment `{environment}`."   |
| Plan application failure      | apply_failure          | "Failed to apply plan.\n{exception}"                     |
| SQLMesh run start             | run_start              | "SQLMesh run started for environment `{environment}`."   |
| SQLMesh run end               | run_end                | "SQLMesh run finished for environment `{environment}`."  |
| SQLMesh run failure           | run_failure            | "Failed to run SQLMesh.\n{exception}"                    |
| Audit failure                 | audit_failure          | "{audit_error}"                                          |

Any combination of these events can be specified in a notification target's `notify_on` field.

## Slack Notifications

SQLMesh supports two types of Slack notification. Slack webhooks can notify a Slack channel, but they cannot message specific users. The Slack Web API can notify channels or users.

### Webhook Configuration

SQLMesh uses Slack's "Incoming Webhooks" for webhook notifications. When you [create an incoming webhook](https://api.slack.com/messaging/webhooks) in Slack, you will receive a unique URL associated with a specific Slack channel. SQLMesh transmits the notification message by submitting a JSON payload to that URL.

This example shows a Slack webhook notification target. Notifications are triggered by plan application start, plan application failure, or SQLMesh run start. The specification uses an environment variable `SLACK_WEBHOOK_URL` instead of hard-coding the URL directly into the configuration file:

=== "YAML"

    ```yaml linenums="1"
    notification_targets:
      - type: slack_webhook
        notify_on:
          - apply_start
          - apply_failure
          - run_start
        url: "{{ env_var('SLACK_WEBHOOK_URL') }}"
    ```

=== "Python"

    ```python linenums="1"
    notification_targets=[
        SlackWebhookNotificationTarget(
            notify_on=["apply_start", "apply_failure", "run_start"],
            url=os.getenv("SLACK_WEBHOOK_URL"),
        )
    ]
    ```

### API Configuration

If you want to notify users, you can use the Slack API notification target. This requires a Slack API token, which can be used for multiple notification targets with different channels or users. See [Slack's official documentation](https://api.slack.com/tutorials/tracks/getting-a-token) for information on getting an API token.

This example shows a Slack API notification target. Notifications are triggered by plan application start, plan application end, or audit failure. The specification uses an environment variable `SLACK_API_TOKEN` instead of hard-coding the token directly into the configuration file:

=== "YAML"

    ```yaml linenums="1"
    notification_targets:
      - type: slack_api
        notify_on:
          - apply_start
          - apply_end
          - audit_failure
        token: "{{ env_var('SLACK_API_TOKEN') }}"
        channel: "UXXXXXXXXX"  # Channel or a user's Slack member ID
    ```

=== "Python"

    ```python linenums="1"
    notification_targets=[
        SlackApiNotificationTarget(
            notify_on=["apply_start", "apply_end", "audit_failure"],
            token=os.getenv("SLACK_API_TOKEN"),
            channel="UXXXXXXXXX",  # Channel or a user's Slack member ID
        )
    ]
    ```

## Email Notifications

SQLMesh supports notifications via email. The notification target specifies the SMTP host, user, password, and sender address. A target may notify multiple recipient email addresses.

This example shows an email notification target, where `sushi@example.com` emails `data-team@example.com` on SQLMesh run failure. The specification uses environment variables `SMTP_HOST`, `SMTP_USER`, and `SMTP_PASSWORD` instead of hard-coding the values directly into the configuration file:

=== "YAML"

    ```yaml linenums="1"
    notification_targets:
      - type: smtp
        notify_on:
          - run_failure
        host: "{{ env_var('SMTP_HOST') }}"
        user: "{{ env_var('SMTP_USER') }}"
        password: "{{ env_var('SMTP_PASSWORD') }}"
        sender: sushi@example.com
        recipients:
          - data-team@example.com
    ```

=== "Python"

    ```python linenums="1"
    notification_targets=[
        BasicSMTPNotificationTarget(
            notify_on=["run_failure"],
            host=os.getenv("SMTP_HOST"),
            user=os.getenv("SMTP_USER"),
            password=os.getenv("SMTP_PASSWORD"),
            sender="notifications@example.com",
            recipients=[
                "data-team@example.com",
            ],
        )
    ]
    ```

## Advanced Usage

### Overriding Notification Targets

In Python configuration files, new notification targets can be configured to send custom messages.

To customize a notification, create a new notification target class as a subclass of one of the three target classes described above (`SlackWebhookNotificationTarget`, `SlackApiNotificationTarget`, or `BasicSMTPNotificationTarget`). See the definitions of these classes on Github [here](https://github.com/SQLMesh/sqlmesh/blob/main/sqlmesh/core/notification_target.py).

Each of those notification target classes is a subclass of `BaseNotificationTarget`, which contains a `notify` function corresponding to each event type. This table lists the notification functions, along with the contextual information available to them at calling time (e.g., the environment name for start/end events):

| Function name        | Contextual information           |
| -------------------- | -------------------------------- |
| notify_apply_start   | Environment name: `env`          |
| notify_apply_end     | Environment name: `env`          |
| notify_apply_failure | Exception stack trace: `exc`     |
| notify_run_start     | Environment name: `env`          |
| notify_run_end       | Environment name: `env`          |
| notify_run_failure   | Exception stack trace: `exc`     |
| notify_audit_failure | Audit error trace: `audit_error` |

This example creates a new notification target class `CustomSMTPNotificationTarget`.

It overrides the default `notify_run_failure` function to read a log file `"/home/sqlmesh/sqlmesh.log"` and append its contents to the exception stack trace `exc`:

=== "Python"

```python
from sqlmesh.core.notification_target import BasicSMTPNotificationTarget

class CustomSMTPNotificationTarget(BasicSMTPNotificationTarget):
    def notify_run_failure(self, exc: str) -> None:
        with open("/home/sqlmesh/sqlmesh.log", "r", encoding="utf-8") as f:
            msg = f"{exc}\n\nLogs:\n{f.read()}"
        super().notify_run_failure(msg)
```

Use this new class by specifying it as a notification target in the configuration file:

=== "Python"

    ```python linenums="1" hl_lines="2"
    notification_targets=[
        CustomSMTPNotificationTarget(
            notify_on=["run_failure"],
            host=os.getenv("SMTP_HOST"),
            user=os.getenv("SMTP_USER"),
            password=os.getenv("SMTP_PASSWORD"),
            sender="notifications@example.com",
            recipients=[
                "data-team@example.com",
            ],
        )
    ]
    ```