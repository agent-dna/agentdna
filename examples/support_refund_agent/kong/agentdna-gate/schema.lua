local typedefs = require "kong.db.schema.typedefs"

return {
  name = "agentdna-gate",
  fields = {
    { protocols = typedefs.protocols_http },
    { config = {
        type = "record",
        fields = {
          { gate_url = { type = "string", required = true } },   -- the gate service, e.g. http://host:8600
          { timeout = { type = "number", default = 10000 } },    -- ms; no answer in time = refused
        },
    } },
  },
}
