import assert from 'node:assert/strict';
import {createHmac, webcrypto} from 'node:crypto';
import test from 'node:test';
import worker from '../worker.js';

globalThis.crypto ??= webcrypto;

const env = {
  SLACK_SIGNING_SECRET: 'test-slack-secret',
  ZOOM_WEBHOOK_SECRET: 'test-zoom-secret',
  PRESIDENT_SLACK_ID: 'PRESIDENT',
  SLACK_APPROVAL_CHANNEL_ID: 'PRIVATE',
  WORKER_SHARED_SECRET: 'test-shared-secret',
  APPS_SCRIPT_URL: 'https://example.test/apps-script',
  SLACK_BOT_TOKEN: 'test-token',
  ZOOM_ACCOUNT_ID: 'ACCOUNT',
  ZOOM_AUTHORIZED_HOST_ID: 'HOST',
  PRIVATE_GH_OWNER: 'example',
  PRIVATE_GH_REPO: 'private',
  GITHUB_DISPATCH_TOKEN: 'test-github-token',
};

function signedRequest(path, payload, platform, valid = true) {
  const body = platform === 'slack' ? new URLSearchParams({payload: JSON.stringify(payload)}).toString() : JSON.stringify(payload);
  const stamp = String(Math.floor(Date.now() / 1000));
  const secret = platform === 'slack' ? env.SLACK_SIGNING_SECRET : env.ZOOM_WEBHOOK_SECRET;
  const signature = 'v0=' + createHmac('sha256', secret).update(`v0:${stamp}:${body}`).digest('hex');
  return new Request(`https://example.test${path}`, {method: 'POST', body, headers: {
    [platform === 'slack' ? 'x-slack-request-timestamp' : 'x-zm-request-timestamp']: stamp,
    [platform === 'slack' ? 'x-slack-signature' : 'x-zm-signature']: valid ? signature : 'v0=invalid',
  }});
}

test('health works without credentials', async () => {
  const response = await worker.fetch(new Request('https://example.test/health'), env, {});
  assert.equal(response.status, 200);
});

test('unsigned approval is rejected', async () => {
  const request = signedRequest('/slack/actions', {}, 'slack', false);
  const response = await worker.fetch(request, env, {});
  assert.equal(response.status, 401);
});

test('signed non-president approval is rejected', async () => {
  const payload = {user: {id: 'MEMBER'}, channel: {id: 'PRIVATE'},
    actions: [{action_id: 'campaign_approve', value: 'weekly-2026-10-13-da182bf9'}]};
  const response = await worker.fetch(signedRequest('/slack/actions', payload, 'slack'), env, {});
  assert.equal(response.status, 403);
});

test('presidential approval reaches private ledger', async () => {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return Response.json({ok: true, state: 'approved'});
  };
  try {
    const tasks = [];
    const ctx = {waitUntil: promise => tasks.push(promise)};
    const payload = {user: {id: 'PRESIDENT'}, channel: {id: 'PRIVATE'},
      actions: [{action_id: 'campaign_approve', value: 'weekly-2026-10-13-da182bf9'}]};
    const response = await worker.fetch(signedRequest('/slack/actions', payload, 'slack'), env, ctx);
    await Promise.all(tasks);
    assert.equal(response.status, 200);
    const handoff = calls.find(call => call.url === env.APPS_SCRIPT_URL);
    assert.equal(JSON.parse(handoff.options.body).action, 'approve');
  } finally {
    globalThis.fetch = original;
  }
});

test('Zoom challenge uses verified signature', async () => {
  const payload = {event: 'endpoint.url_validation', payload: {plainToken: 'challenge'}};
  const response = await worker.fetch(signedRequest('/zoom/events', payload, 'zoom'), env, {});
  const answer = await response.json();
  assert.equal(answer.plainToken, 'challenge');
  assert.equal(answer.encryptedToken, createHmac('sha256', env.ZOOM_WEBHOOK_SECRET).update('challenge').digest('hex'));
});
