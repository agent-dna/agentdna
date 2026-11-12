-- env-key: after the AgentDNA gate said yes, put the app's key on the call.
--
-- The key is in an environment variable (kong.yml: key = "{vault://env/...}"),
-- so it never sits in a file, and agents never hold it. A call the gate did
-- not check and allow gets no key, so the app refuses it.

local EnvKey = { PRIORITY = 850, VERSION = "0.1.0" }  -- right after agentdna-gate (900)

function EnvKey:access(conf)
  if not kong.ctx.shared.agentdna then
    return  -- the gate did not say yes: no key
  end
  kong.service.request.set_header(conf.header, conf.key)
end

return EnvKey
