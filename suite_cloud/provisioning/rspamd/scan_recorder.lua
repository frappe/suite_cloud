-- Suite Cloud: one JSON line per message a signed-in sender sends, for the outbound scan
-- dashboards, and the SMTP reply of a refused message. suite-cloud-scans.timer sends the lines to
-- Suite Cloud. Written by install-rspamd.yml: do not edit here.
local rspamd_logger = require "rspamd_logger"
local rspamd_util = require "rspamd_util"
local ucl = require "ucl"

local SPOOL = "/var/lib/rspamd/suite-cloud-scans.jsonl"
local LINE_BUFFER = 65536 -- one write per line, so the proxy processes never interleave lines
local REPLY_RULES = 3
local DESCRIPTION_LENGTH = 80 -- keeps the reply under the 512 bytes an SMTP reply line allows
local TIMEOUT_MODULE = 'task timeout'
local TIMEOUT_REPLY = 'Message not accepted: the spam check could not finish in time. '
  .. 'Send it again in a few minutes. If this keeps happening, contact your administrator.'

local function first_address(addresses)
  if addresses and addresses[1] and addresses[1].addr then
    return addresses[1].addr
  end
  return nil
end

local function get_symbol_scores(task)
  local scores = {}
  for _, symbol in ipairs(task:get_symbols_all() or {}) do
    scores[symbol.name] = symbol.score
  end
  return scores
end

local function get_record(task, user)
  local ip = task:get_from_ip()
  local content = task:get_content()
  return {
    time = rspamd_util.get_time(),
    user = user,
    envelope_from = first_address(task:get_from('smtp')),
    header_from = first_address(task:get_from('mime')),
    message_id = task:get_message_id(),
    queue_id = task:get_queue_id(),
    client_ip = (ip and ip:is_valid()) and tostring(ip) or nil,
    recipients = #(task:get_recipients('smtp') or {}),
    size = content and content:len() or 0,
    action = task:get_metric_action(),
    score = task:get_metric_score()[1],
    scan_time = math.floor(task:get_scan_time() * 1000),
    symbols = get_symbol_scores(task),
  }
end

local function record_scan(task)
  local user = task:get_user()
  if not user then
    return
  end
  local file, problem = io.open(SPOOL, 'a')
  if not file then
    rspamd_logger.errx(task, 'cannot open %s: %s', SPOOL, problem)
    return
  end
  file:setvbuf('full', LINE_BUFFER)
  file:write(ucl.to_format(get_record(task, user), 'json-compact'), '\n')
  file:close()
end

-- An SMTP reply holds printable ASCII only.
local function to_reply_text(text, length)
  local clean = tostring(text):gsub('[^\32-\126]', ' '):gsub('%s+', ' '):match('^ *(.-) *$')
  if #clean > length then
    clean = clean:sub(1, length - 3) .. '...'
  end
  return clean
end

-- The rules that added the most to the score, highest first.
local function get_top_rules(task)
  local rules = {}
  for _, symbol in ipairs(task:get_symbols_all() or {}) do
    if symbol.score > 0 then
      rules[#rules + 1] = symbol
    end
  end
  table.sort(rules, function(a, b)
    if a.score ~= b.score then
      return a.score > b.score
    end
    return a.name < b.name
  end)
  return { unpack(rules, 1, math.min(#rules, REPLY_RULES)) }
end

local function describe_rule(symbol)
  local text = string.format('%s (+%.2f)', symbol.name, symbol.score)
  local config = rspamd_config:get_symbol(symbol.name)
  if config and config.description and config.description ~= '' then
    text = text .. ': ' .. to_reply_text(config.description, DESCRIPTION_LENGTH)
  end
  return text
end

-- soft_reject_on_timeout defers a scan that ran out of time. Its own reply is
-- "timeout processing message", which a sender cannot act on.
local function set_timeout_reply(task)
  local _, _, _, module = task:has_pre_result()
  if module == TIMEOUT_MODULE then
    task:append_message(TIMEOUT_REPLY, 'smtp_message')
  end
end

-- The milter sends smtp_message as the reply instead of its reject_message.
local function set_reject_reply(task)
  local action = task:get_metric_action()
  if action == 'soft reject' then
    return set_timeout_reply(task)
  elseif action ~= 'reject' then
    return
  end
  local scores = task:get_metric_score()
  local reply = string.format('Message refused as spam: score %.2f, limit %g.', scores[1], scores[2])
  local rules = {}
  for _, symbol in ipairs(get_top_rules(task)) do
    rules[#rules + 1] = describe_rule(symbol)
  end
  if #rules > 0 then
    reply = string.format('%s Top rules: %s.', reply, table.concat(rules, '; '))
  end
  task:append_message(reply .. ' Contact your administrator if this is wrong.', 'smtp_message')
end

rspamd_config:register_symbol({
  name = 'SUITE_CLOUD_REJECT_REPLY',
  type = 'idempotent',
  callback = set_reject_reply,
  flags = 'empty,explicit_disable,ignore_passthrough',
})

rspamd_config:register_symbol({
  name = 'SUITE_CLOUD_SCAN_RECORD',
  type = 'idempotent',
  callback = record_scan,
  flags = 'empty,explicit_disable,ignore_passthrough',
})
