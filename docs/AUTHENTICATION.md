# Authentication Research Findings

This document records facts found in the Eurofurence Identity documentation,
the live OpenID Connect discovery document, and the relevant Creator System
GitHub issues. It does not define Creator System architecture or propose an
implementation. Architecture decisions are documented in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

The live metadata below was observed on 2026-09-02.

## Source Status

The [Identity API v2 documentation][identity-v2] is marked as a technical
preview for an unreleased version and is subject to change.

The live discovery document describes the capabilities currently advertised by
the production Identity Provider. It does not yet match every part of the v2
preview.

Creator System issues [#8][issue-8], [#9][issue-9], [#10][issue-10],
[#11][issue-11], and [#18][issue-18] are open. They contain no comments or
additional stakeholder decisions.

## Live OIDC Metadata

The live [OpenID Connect discovery document][live-discovery] advertises:

| Field | Value |
| --- | --- |
| Issuer | `https://identity.eurofurence.org/` |
| Authorization endpoint | `https://identity.eurofurence.org/oauth2/auth` |
| Token endpoint | `https://identity.eurofurence.org/oauth2/token` |
| JWKS URI | `https://identity.eurofurence.org/.well-known/jwks.json` |
| UserInfo endpoint | `https://identity.eurofurence.org/api/v1/userinfo` |
| Revocation endpoint | `https://identity.eurofurence.org/oauth2/revoke` |
| End-session endpoint | `https://identity.eurofurence.org/oauth2/sessions/logout` |

The advertised grant types are:

* `authorization_code`
* `implicit`
* `client_credentials`
* `refresh_token`

The advertised PKCE methods are `plain` and `S256`.

The advertised token endpoint authentication methods are:

* `client_secret_post`
* `client_secret_basic`
* `private_key_jwt`
* `none`

ID tokens are advertised as RS256-signed. The advertised claims are `sub`,
`email`, `name`, `email_verified`, `avatar`, and `groups`. Discovery
metadata lists supported claims; it does not guarantee that every claim is
present in every token or UserInfo response.

The live provider advertises front-channel and back-channel logout, including
session-aware variants.

## Live Scopes

The live discovery document advertises these scopes:

```text
offline_access
offline
openid
profile
email
groups
groups.read
groups.write
groups.update
groups.delete
registration.reg.test
registration.reg.live
registration.room.test
registration.room.live
nextcloud
view_full_staff_details
```

The v2 registration scopes, including `registration.my.read.basic`, are not in
the live discovery document.

## Documented User Login Flow

The [v2 OAuth and OIDC primer][oidc-primer] documents Authorization Code with
PKCE for applications where a person signs in. It says PKCE is required for
every client. It documents Client Credentials for service-to-service access and
refresh tokens as a continuation of Authorization Code when `offline_access`
was requested.

The same preview says that Device Code and Implicit are not supported by the
new version. This differs from the current live discovery document, which still
advertises Implicit.

## Identity Claims

The [scope documentation][scopes] defines these identity scope mappings:

| Scope | Documented data |
| --- | --- |
| `openid` | `sub` |
| `profile` | `name`, `avatar` |
| `email` | `email`, `email_verified` |
| `groups` | The user's group memberships |
| `offline_access` | A refresh token |

The [token documentation][tokens] identifies `sub` as the stable user
identifier and says not to use email or name for that purpose because they can
change.

The documented ID token example includes standard token claims such as `iss`,
`sub`, `aud`, `exp`, and `iat`, plus `sid` for logout. Additional
identity claims depend on the granted scopes.

The access token is documented as opaque and intended for API calls. The ID
token is documented as a signed JWT intended for the client and must not be
sent to APIs as a bearer token.

## UserInfo

The current live discovery document points to `/api/v1/userinfo`. The current
v1 schema contains:

* `sub`
* `name`
* `email`
* `email_verified`
* `avatar`
* `groups`, represented as an array of group identifier strings

The [v2 UserInfo schema][userinfo-v2] documents `/api/v2/userinfo` with:

* `sub`
* `name`
* `email`
* `email_verified`
* `avatar`
* `groups`, represented as membership objects

Each documented v2 group membership object contains:

* `id`
* `name`
* `type`
* `slug`
* `level`
* nullable `title`

The documented v2 group levels are `member`, `team_lead`, `director`, and
`division_director`.

The v2 schema also contains staff profile fields gated by staff scopes. The
Identity documentation does not state that having a staff profile or a
particular group level grants a Creator System role.

## Registration Data

The v2 scope documentation describes `registration.my.read.basic` as access to
the authenticated user's:

* registration ID
* registration status
* nickname
* attending flag

These values are documented as registration data, not as OIDC identity claims.

The v2 documentation also lists broader registration scopes, including
`registration.my.read`, granular write scopes, and privileged
`registration.all.*` scopes.

The reviewed v2 Identity API documentation does not provide a concrete endpoint
or response schema for retrieving current-user registration data with
`registration.my.read.basic`.

The [app-to-app guide][app-to-app] documents Registration as a separate resource
service with audience `org.eurofurence.registration`. Its example uses a
Client Credentials token with `registration.all.read`. That example does not
specify how a user-bearing Authorization Code token retrieves the authenticated
user's registration.

## Eligibility

The reviewed documentation does not define:

* the possible registration status values
* which status means fully paid
* which package, payment, or ticket fields establish attendee eligibility
* whether `status` and `attending` are sufficient for an eligibility decision
* when eligibility should be checked again after login

No registration-status-to-eligibility mapping was found.

## Groups and Authorization Inputs

The `groups` scope is documented as supplying the signed-in user's memberships
without granting group API access. `groups.read` is documented separately for
reading group details and memberships through the API.

The scope documentation explicitly states that scopes are not application
permissions. The reviewed Identity documentation and Creator System issues do
not define:

* a group that represents Creator System administrators
* a group that represents badge pickup staff
* a group-level-to-role mapping
* whether Creator System roles should be local or derived from Identity groups

## Logout and Sessions

The [user-facing application guide][build-app] documents OIDC back-channel
logout. It says the provider sends a form-encoded `logout_token` to a
registered back-channel logout URI. The application validates that token and
uses its `sid` claim to find and destroy the corresponding local session.

The live discovery document also advertises an end-session endpoint and
front-channel logout. The reviewed documentation does not define the Creator
System's local session lifetime or whether a Creator System logout should also
initiate provider-wide logout.

## Refresh Tokens

The token documentation says refresh tokens are issued when `offline_access`
is requested. They are long-lived secrets, must be stored server-side, and may
be rotated when exchanged. If a replacement refresh token is returned, the old
token must be discarded.

The reviewed requirements and issues do not identify a confirmed Creator System
workflow that requires a refresh token.

## Preview v2 Notification Service

The [v2 notification service documentation][notification-service] describes a
central service that first-party OAuth applications can use to send
notifications to individual Identity users through:

* email
* Telegram
* the Identity in-app notification feed

This notification service is part of the unreleased v2 technical preview. The
live discovery document does not advertise the documented
`notifications.send` scope, so its current production availability is not
established by the reviewed sources.

### Access

The documented notification flow uses the application's identity, not a user's
login token:

1. An Identity administrator enables `allow_notifications` for the OAuth
   application.
2. The application registers its notification types in the Identity portal.
3. The application obtains a Client Credentials access token with the
   `notifications.send` scope.
4. The application sends a notification using that access token.

No user access token or user refresh token is documented as part of this flow.
After notification access is enabled, the preview says a Notifications tab is
available under `My Apps` for managing the application's notification types.

### Notification Types

Every notification references a type registered under the sending OAuth
application. A type has:

* a key that is unique per application and immutable after creation
* a display name
* an optional description
* a category that is immutable after creation
* one or more default channels

The documented categories behave as follows:

| Category | User control |
| --- | --- |
| Transactional | Cannot be disabled; always uses its default channels and must include email |
| Operational | Enabled by default; users can select channels |
| Informational | Users can select channels or disable the type |
| Promotional | Disabled by default; users must opt in |

### Send Endpoint

The documented request is:

```http
POST https://identity.eurofurence.org/api/v2/notifications
Authorization: Bearer ACCESS_TOKEN
Content-Type: application/json
```

The [request schema][notification-request] contains:

| Field | Required | Documented constraints |
| --- | --- | --- |
| `type` | Yes | A type key registered for the sending application |
| `user_id` | Yes | The recipient's Identity user ID |
| `subject` | Yes | Plain text, at most 255 characters |
| `body` | Yes | Plain text, at most 10,000 characters |
| `html` | No | Nullable HTML used only for email |
| `cta` | No | Nullable object; `label` and `url` must be supplied together |
| `cta.label` | With `cta` | At most 255 characters |
| `cta.url` | With `cta` | URI of at most 2,048 characters |

The plain-text `body` is used for Telegram and in-app delivery. It is also the
email fallback when `html` is absent. When `html` is present, email uses it
instead of `body`; the other channels continue to use `body`.

The preview says `body` does not support Markdown or HTML. A blank line
separates paragraphs. A single newline inside a paragraph is preserved by
Telegram but collapses to a space in email. The optional `html` value is
rendered verbatim by the email channel, while Telegram and in-app delivery
continue to use the required plain-text `body`.

The documentation says the endpoint queues notifications asynchronously. A
successful request returns `202 Accepted` with an empty body.

### Channel Selection

For each notification, Identity:

1. starts with the notification type's default channels
2. applies the user's per-type channel overrides
3. applies the user's master channel switches

Transactional types restore their complete default channel set and ignore user
preferences. If Telegram is selected but the user has not linked a Telegram
account, Telegram delivery is skipped without preventing delivery through other
selected channels.

### Limits and Errors

The preview documents a limit of 60 notifications per application per minute.
Rate-limited responses use `429 Too Many Requests` and include a
`Retry-After` header.

The [send endpoint documentation][send-notification] defines:

| Status | Meaning |
| --- | --- |
| `202` | Accepted and queued for delivery |
| `403` | Missing `notifications.send` or notifications are not enabled for the application |
| `404` | Notification type or user not found |
| `422` | Request validation failed |
| `429` | Rate limit exceeded |

The reviewed documentation does not establish:

* whether the v2 notification endpoint is currently enabled in production
* whether the Creator System is eligible for first-party notification access
* whether `allow_notifications` has been enabled for a Creator System client
* which Creator System notification types have been approved or registered

## Client Registration and Development

The user-facing application guide says an OAuth client registration requires:

* application name
* redirect URIs
* requested scopes
* a privacy policy URL for an external application

It directs new client requests to Thiritin. The live provider supports both
confidential-client authentication methods and clients using no token endpoint
authentication.

The reviewed documentation does not specify:

* a Creator System client ID or client secret
* registered production callback or logout URIs
* a separate development client
* whether localhost redirect URIs are accepted
* which scopes will be approved for the Creator System clients
* whether the current provisioning contact differs from the documented contact

[identity-v2]: https://identity-btv.pages.dev/identity/api/v2/eurofurence-identity
[live-discovery]: https://identity.eurofurence.org/.well-known/openid-configuration
[oidc-primer]: https://identity-btv.pages.dev/identity/concepts/oauth-oidc-primer
[scopes]: https://identity-btv.pages.dev/identity/concepts/scopes
[tokens]: https://identity-btv.pages.dev/identity/concepts/tokens
[userinfo-v2]: https://identity-btv.pages.dev/identity/api/v2/schemas/userinfo
[build-app]: https://identity-btv.pages.dev/identity/integration/build-an-application
[app-to-app]: https://identity-btv.pages.dev/identity/integration/app-to-app
[notification-service]: https://identity-btv.pages.dev/identity/platform-services/notification-service
[notification-request]: https://identity-btv.pages.dev/identity/api/v2/schemas/sendnotificationrequest
[send-notification]: https://identity-btv.pages.dev/identity/api/v2/send-notification
[issue-8]: https://github.com/eurofurence/creators.eurofurence.org/issues/8
[issue-9]: https://github.com/eurofurence/creators.eurofurence.org/issues/9
[issue-10]: https://github.com/eurofurence/creators.eurofurence.org/issues/10
[issue-11]: https://github.com/eurofurence/creators.eurofurence.org/issues/11
[issue-18]: https://github.com/eurofurence/creators.eurofurence.org/issues/18
