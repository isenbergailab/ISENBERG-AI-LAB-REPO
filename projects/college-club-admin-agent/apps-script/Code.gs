/* Bound to private interest-response Sheet. Install trigger using Lab Gmail. */

const AGENT_HEADERS = ['Agent Status', 'Agent Attempts', 'Agent Last Error', 'Agent Sent At'];
const CAMPAIGN_HEADERS = ['id','kind','title','state','created_on','updated_at','approved_by','approved_at','deadline','package_path','instagram_url','linkedin_url','x_url','substack_url','slack_url'];

function property_(name) {
  const value = PropertiesService.getScriptProperties().getProperty(name);
  if (!value) throw new Error('Missing Script Property: ' + name);
  return value;
}

function columns_(sheet) {
  let values = sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), 1)).getValues()[0];
  AGENT_HEADERS.forEach(name => {
    if (!values.includes(name)) {
      sheet.getRange(1, values.length + 1).setValue(name);
      values.push(name);
    }
  });
  const map = {};
  values.forEach((value, index) => { map[String(value).trim().toLowerCase()] = index + 1; });
  return map;
}

function onInterestFormSubmit(e) {
  if (!e || !e.range) throw new Error('Install a Sheets form-submit trigger');
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sheet = e.range.getSheet();
    const row = e.range.getRow();
    const map = columns_(sheet);
    const emailHeader = (PropertiesService.getScriptProperties().getProperty('EMAIL_COLUMN_NAME') || 'Email Address').toLowerCase();
    const emailColumn = map[emailHeader];
    if (!emailColumn) throw new Error('Email column missing: ' + emailHeader);
    const email = String(sheet.getRange(row, emailColumn).getValue()).trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error('Invalid interest-form email');
    const statusColumn = map['agent status'];
    const attemptsColumn = map['agent attempts'];
    const errorColumn = map['agent last error'];
    const sentColumn = map['agent sent at'];
    const status = String(sheet.getRange(row, statusColumn).getValue());
    if (status === 'SENT' || status === 'DUPLICATE' || status === 'ATTEMPTING') return;
    const previous = sheet.getRange(2, 1, Math.max(row - 2, 1), sheet.getLastColumn()).getValues();
    for (let i = 0; i < row - 2; i++) {
      if (String(previous[i][emailColumn - 1]).trim().toLowerCase() === email &&
          ['SENT','ATTEMPTING'].includes(String(previous[i][statusColumn - 1]))) {
        sheet.getRange(row, statusColumn).setValue('DUPLICATE');
        return;
      }
    }
    sheet.getRange(row, statusColumn).setValue('ATTEMPTING');
    SpreadsheetApp.flush();
    const body = [
      'Join Slack before your first meeting.',
      '',
      'Welcome to Isenberg AI Lab. Meetings are Tuesdays, 5:15–6 PM. Demos and projects are optional.',
      'Slack is our club communication channel: ' + property_('SLACK_JOIN_URL'),
      'Read the current new-member guide: ' + property_('GUIDE_URL'),
      'Explore the Lab repository: ' + property_('REPO_URL'),
      'Review the club guardrails: ' + property_('GUARDRAILS_URL'),
      'Questions? Email isenbergailab@gmail.com.'
    ].join('\n');
    let lastError = '';
    for (let attempt = 1; attempt <= 2; attempt++) {
      sheet.getRange(row, attemptsColumn).setValue(attempt);
      try {
        if (MailApp.getRemainingDailyQuota() < 1) throw new Error('Daily email quota exhausted');
        MailApp.sendEmail(email, 'Welcome to Isenberg AI Lab', body, {name: 'Isenberg AI Lab'});
        sheet.getRange(row, statusColumn).setValue('SENT');
        sheet.getRange(row, sentColumn).setValue(new Date());
        sheet.getRange(row, errorColumn).clearContent();
        return;
      } catch (error) {
        lastError = String(error).slice(0, 250);
      }
    }
    sheet.getRange(row, statusColumn).setValue('FAILED_REVIEW');
    sheet.getRange(row, errorColumn).setValue(lastError);
    MailApp.sendEmail('isenbergailab@gmail.com', 'Lab onboarding needs review',
      'An onboarding email failed twice. Check response row ' + row + ' in the private interest Sheet.');
  } finally {
    lock.releaseLock();
  }
}

function json_(object) {
  return ContentService.createTextOutput(JSON.stringify(object)).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  const data = JSON.parse(e.postData.contents || '{}');
  if (data.secret !== property_('WORKER_SHARED_SECRET')) return json_({ok:false,error:'unauthorized'});
  if (data.action === 'alert') {
    const message = String(data.message || '').slice(0, 250);
    if (!message) return json_({ok:false,error:'empty_alert'});
    MailApp.sendEmail('isenbergailab@gmail.com', 'Lab agent needs review', message);
    return json_({ok:true});
  }
  if (data.user_id !== property_('PRESIDENT_SLACK_ID')) return json_({ok:false,error:'wrong_approver'});
  if (data.channel_id !== property_('SLACK_APPROVAL_CHANNEL_ID')) return json_({ok:false,error:'wrong_channel'});
  if (!['approve','skip'].includes(data.action)) return json_({ok:false,error:'wrong_action'});
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const book = SpreadsheetApp.openById(property_('CAMPAIGN_SHEET_ID'));
    const sheet = book.getSheetByName('Campaigns');
    const rows = sheet.getDataRange().getValues();
    if (JSON.stringify(rows[0]) !== JSON.stringify(CAMPAIGN_HEADERS)) return json_({ok:false,error:'schema_mismatch'});
    for (let i = 1; i < rows.length; i++) {
      if (rows[i][0] !== data.campaign_id) continue;
      if (rows[i][3] !== 'awaiting_approval') return json_({ok:false,error:'state_changed'});
      const deadline = rows[i][8];
      if (deadline && new Date(deadline).getTime() < Date.now()) {
        sheet.getRange(i + 1, 4).setValue('skipped');
        return json_({ok:false,error:'approval_expired'});
      }
      const now = new Date().toISOString();
      sheet.getRange(i + 1, 4).setValue(data.action === 'approve' ? 'approved' : 'skipped');
      sheet.getRange(i + 1, 6).setValue(now);
      if (data.action === 'approve') {
        sheet.getRange(i + 1, 7).setValue(data.user_id);
        sheet.getRange(i + 1, 8).setValue(now);
      }
      return json_({ok:true,state:data.action === 'approve' ? 'approved' : 'skipped'});
    }
    return json_({ok:false,error:'campaign_missing'});
  } finally {
    lock.releaseLock();
  }
}
