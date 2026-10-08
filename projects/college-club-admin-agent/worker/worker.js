/* Public endpoint. Secrets are Cloudflare bindings, never repository values. */

const encoder = new TextEncoder();

async function hmac(secret, value) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  const bytes = new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(value)));
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

function equal(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function fresh(header) {
  const stamp = Number(header);
  return Number.isInteger(stamp) && Math.abs(Math.floor(Date.now() / 1000) - stamp) <= 300;
}

async function verifySlack(request, raw, env) {
  const stamp = request.headers.get('x-slack-request-timestamp');
  if (!fresh(stamp)) return false;
  const expected = 'v0=' + await hmac(env.SLACK_SIGNING_SECRET, `v0:${stamp}:${raw}`);
  return equal(expected, request.headers.get('x-slack-signature'));
}

async function verifyZoom(request, raw, env) {
  const stamp = request.headers.get('x-zm-request-timestamp');
  if (!fresh(stamp)) return false;
  const expected = 'v0=' + await hmac(env.ZOOM_WEBHOOK_SECRET, `v0:${stamp}:${raw}`);
  return equal(expected, request.headers.get('x-zm-signature'));
}

async function privateAlert(env, message) {
  await Promise.allSettled([fetch('https://slack.com/api/chat.postMessage', {
    method: 'POST',
    headers: {'Authorization': `Bearer ${env.SLACK_BOT_TOKEN}`, 'Content-Type': 'application/json'},
    body: JSON.stringify({channel: env.SLACK_APPROVAL_CHANNEL_ID, text: message}),
  }), fetch(env.APPS_SCRIPT_URL, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'alert', secret: env.WORKER_SHARED_SECRET, message}),
  })]);
}

async function forwardApproval(env, body) {
  try {
    const response = await fetch(env.APPS_SCRIPT_URL, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...body, secret: env.WORKER_SHARED_SECRET})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `Approval handoff failed: ${response.status}`);
    await privateAlert(env, `Campaign ${body.campaign_id}: ${result.state} by president.`);
  } catch (error) {
    await privateAlert(env, `Approval handoff failed for ${body.campaign_id}. Check private ledger. ${String(error).slice(0, 100)}`);
  }
}

async function slackAction(request, env, ctx) {
  const raw = await request.text();
  if (!(await verifySlack(request, raw, env))) return new Response('Unauthorized', {status: 401});
  const payload = JSON.parse(new URLSearchParams(raw).get('payload') || '{}');
  const action = payload.actions?.[0];
  const name = action?.action_id === 'campaign_approve' ? 'approve' : action?.action_id === 'campaign_skip' ? 'skip' : '';
  if (!name || payload.user?.id !== env.PRESIDENT_SLACK_ID || payload.channel?.id !== env.SLACK_APPROVAL_CHANNEL_ID)
    return new Response('Forbidden', {status: 403});
  const id = action.value;
  if (!/^[a-z0-9-]{1,90}$/.test(id)) return new Response('Bad campaign', {status: 400});
  ctx.waitUntil(forwardApproval(env, {action: name, campaign_id: id, user_id: payload.user.id, channel_id: payload.channel.id}));
  return Response.json({response_type: 'ephemeral', text: 'Decision received. Check private ledger confirmation.'});
}

async function dispatchZoom(env, meetingUuid) {
  const response = await fetch(`https://api.github.com/repos/${env.PRIVATE_GH_OWNER}/${env.PRIVATE_GH_REPO}/dispatches`, {
    method: 'POST',
    headers: {'Authorization': `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, 'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json', 'User-Agent': 'college-club-admin-agent'},
    body: JSON.stringify({event_type: 'zoom-recording-ready', client_payload: {meeting_uuid: meetingUuid}}),
  });
  if (!response.ok) throw new Error(`Private recording dispatch failed: ${response.status}`);
}

async function zoomEvent(request, env, ctx) {
  const raw = await request.text();
  if (!(await verifyZoom(request, raw, env))) return new Response('Unauthorized', {status: 401});
  const event = JSON.parse(raw);
  if (event.event === 'endpoint.url_validation') {
    const plainToken = event.payload?.plainToken;
    if (!plainToken) return new Response('Bad challenge', {status: 400});
    return Response.json({plainToken, encryptedToken: await hmac(env.ZOOM_WEBHOOK_SECRET, plainToken)});
  }
  if (event.event !== 'recording.completed') return new Response(null, {status: 204});
  if (event.payload?.account_id !== env.ZOOM_ACCOUNT_ID || event.payload?.object?.host_id !== env.ZOOM_AUTHORIZED_HOST_ID)
    return new Response('Unauthorized host', {status: 403});
  const uuid = event.payload?.object?.uuid;
  if (typeof uuid !== 'string' || uuid.length > 200) return new Response('Bad meeting', {status: 400});
  ctx.waitUntil(dispatchZoom(env, uuid).catch(() => privateAlert(env, 'Zoom recording dispatch failed. Check private runner.')));
  return new Response(null, {status: 204});
}

export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (request.method === 'GET' && path === '/health') return new Response('ok');
    if (request.method !== 'POST') return new Response('Method not allowed', {status: 405});
    try {
      if (path === '/slack/actions') return await slackAction(request, env, ctx);
      if (path === '/zoom/events') return await zoomEvent(request, env, ctx);
      return new Response('Not found', {status: 404});
    } catch (_) {
      return new Response('Invalid request', {status: 400});
    }
  },
};
