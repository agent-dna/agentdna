-- agentdna-gate: Kong asks the AgentDNA gate service about every call that
-- carries a workflow, then does what it says. All AgentDNA work happens in the gate
-- service; this only carries the answers in and out. A call with no workflow isn't
-- AgentDNA's: it goes on as it would without this plugin. A body too big to read
-- is refused (413).
--
-- Before forwarding (/check):
--   yes -> forward the signed method, content type and body (never the request's own)
--   no  -> answer the caller with the gate's status (the gate already recorded it)
--   gate unreachable or broken -> refuse (fail closed)
--
-- After the app answers (/answered), whatever the answer:
--   success -> send the caller exactly the body the gate returns: the workflow, with the
--              app's answer signed inside
--   error   -> the gate records it; the app's answer passes through
--   gate unreachable -> the app has already done the work, so its answer passes
--              through unsigned. The caller's client refuses it as unsigned, but
--              keeps it on the refusal, so the caller can look before retrying.

local http = require "resty.http"
local cjson = require "cjson.safe"

local AgentDNAGate = { PRIORITY = 900, VERSION = "0.2.0" }

local function post(url, timeout, data)
  local client = http.new()
  client:set_timeout(timeout)
  local reply, err = client:request_uri(url, {
    method = "POST",
    body = cjson.encode(data),
    headers = { ["Content-Type"] = "application/json" },
  })
  if not reply or reply.status ~= 200 then
    return nil, err or ("status " .. reply.status)
  end
  return cjson.decode(reply.body)
end

function AgentDNAGate:access(conf)
  -- The path exactly as the agent sent (and signed) it, with the query string
  local path = kong.request.get_raw_path()
  local query = kong.request.get_raw_query()
  if query ~= "" then
    path = path .. "?" .. query
  end

  local body, err = kong.request.get_raw_body()
  if not body then  -- bigger than nginx_http_client_body_buffer_size
    return kong.response.exit(413, { error = "the call is too big for the gateway: " .. (err or "") })
  end

  local data = cjson.decode(body)
  if type(data) ~= "table" or type(data.agentdna) ~= "table" then
    return  -- no workflow: not an AgentDNA call, so the route's own settings decide
  end

  local verdict = post(conf.gate_url .. "/check", conf.timeout, {
    path = path,
    body = body,
    headers = kong.request.get_headers(),  -- what the caller sent: for the credential row (server_in)
  })
  if not verdict then
    return kong.response.exit(503, { error = "the AgentDNA gate service did not answer" })
  end
  if verdict.allowed ~= true then
    return kong.response.exit(verdict.status, { error = verdict.reason, code = verdict.code })
  end

  kong.ctx.plugin.workflow = verdict.workflow
  kong.ctx.shared.agentdna = { workflow = verdict.workflow, run_id = verdict.run_id }  -- for plugins after this one
  kong.service.request.enable_buffering()  -- hold the app's answer, to hand it to the gate
  kong.service.request.clear_header("Accept-Encoding")  -- the gate signs the answer as text: no compression
  kong.service.request.set_method(verdict.method)
  if verdict.encoding == "base64" then  -- a binary body (a PDF): forward the bytes
    kong.service.request.set_raw_body(ngx.decode_base64(verdict.body))
  else
    kong.service.request.set_raw_body(verdict.body)
  end
  if verdict.content_type ~= "" then
    kong.service.request.set_header("Content-Type", verdict.content_type)
  else
    kong.service.request.clear_header("Content-Type")
  end
end

function AgentDNAGate:response(conf)
  if not kong.ctx.plugin.workflow then
    return
  end

  local result, err = post(conf.gate_url .. "/answered", conf.timeout, {
    workflow = kong.ctx.plugin.workflow,
    status = kong.service.response.get_status(),
    content_type = kong.service.response.get_header("Content-Type") or "",
    body = ngx.encode_base64(kong.service.response.get_raw_body() or ""),  -- base64: binary answers too
    encoding = "base64",
    -- what Kong sent the app, and where: for the credential row (server_out)
    headers = ngx.req.get_headers(),
    destination = ngx.var.upstream_scheme .. "://" .. ngx.var.upstream_host .. ngx.var.upstream_uri,
  })

  if result and result.body then
    return kong.response.exit(kong.service.response.get_status(), cjson.encode(result.body),
                              { ["Content-Type"] = "application/json" })
  elseif not result then
    kong.log.err("agentdna-gate: /answered failed, the app's answer passes through unsigned: ", err)
  end
end

return AgentDNAGate
