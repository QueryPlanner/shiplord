# Provisional protocol framing

The helper, SSH transport, and deployment operations are not implemented. This
specification is provisional and must be tested on disposable Linux systems
before a privileged helper is installed.

Each metadata message is a four-byte unsigned big-endian length followed by that
number of UTF-8 JSON bytes. The length must be between 1 and 1,048,576 bytes,
for requests and responses alike. The JSON value must be an object. Duplicate
keys, non-finite numbers, malformed UTF-8, and truncated frames are invalid.
Framing reads exactly one message and leaves any later bytes unread. A future
transport must define message sequencing, deadlines, and trailing-byte handling.
Binary uploads remain unsupported until their framing, limits, and digest checks
are defined and tested.

Before any future remote action, the client must request capabilities on the
same connection. A successful capability response contains `status: "ok"`,
`request_id`, integer `version`, `helper_version`, and an `actions` array. The
client checks the request ID, protocol version, and requested action. A helper
that cannot support the requested protocol returns `status: "incompatible"`,
`request_id`, and a nonempty `supported_versions` array of positive integers.
No helper may advertise an action it cannot actually perform.

The helper must independently validate each request's version, action, and
application authorization. A capability response does not authorize later
requests. The transport must keep stdout free of diagnostics and enforce finite
read and write deadlines. The current framing functions do not implement these
transport or authorization requirements.
