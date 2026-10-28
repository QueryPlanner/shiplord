# Provisional protocol framing

An unprivileged capabilities-only prototype is available as
`python -m shiplord.host`. The SSH transport supports only the one-shot
capabilities check. Deployment operations are not implemented. This specification
is provisional and must be tested on disposable Linux systems before a
privileged helper is installed.

Each metadata message is a four-byte unsigned big-endian length followed by that
number of UTF-8 JSON bytes. The length must be between 1 and 1,048,576 bytes,
for requests and responses alike. The JSON value must be an object. Duplicate
keys, non-finite numbers, malformed UTF-8, and truncated frames are invalid.
Framing reads exactly one message and leaves any later bytes unread. The SSH
capabilities transport enforces a deadline and rejects trailing output. A future
deployment transport must define its own message sequence and deadlines.
Binary uploads remain unsupported until their framing, limits, and digest checks
are defined and tested.

The prototype accepts one request frame, followed by EOF on stdin. It rejects
trailing bytes and emits one response frame, followed by EOF on stdout. The
caller must close stdin after sending the request. The prototype enforces a
15-second overall deadline and its caller also supplies a finite timeout. It is
not an installed dispatcher and must not be used as a privileged entry point.
Server-scoped capability requests use `application_id: null`. Other actions
require an application ID. A server-scoped response grants no application access.

Before any future remote action, the client must request capabilities on the
same connection. A successful capability response contains `status: "ok"`,
`request_id`, integer `version`, `helper_version`, and an `actions` array. The
client checks the request ID, protocol version, and requested action. A helper
that cannot support the requested protocol returns `status: "incompatible"`,
`request_id`, and a nonempty `supported_versions` array of positive integers.
No helper may advertise an action it cannot actually perform.
The prototype advertises only `capabilities` and rejects all other actions. It
requires an empty arguments object and a null commit for capability requests.
It exits with code 4 after an incompatible-version response. Malformed requests
and unsupported actions return a nonzero status. The prototype's one-shot
exchange does not yet define a same-connection action following the handshake.

The helper must independently validate each request's version, action, and
application authorization. A capability response does not authorize later
requests. The transport must keep stdout free of diagnostics and enforce finite
read and write deadlines. The framing functions do not enforce deadlines or
authorization themselves.
