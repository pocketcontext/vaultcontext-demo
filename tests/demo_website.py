"""Offline demo UI checks. All live-mode requests use synthetic intercepted routes.
Run: uv run --with playwright==1.60.0 python tests/demo_website.py
Set PLAYWRIGHT_EXECUTABLE_PATH only when using an already installed browser.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path
import json
import hashlib
import base64
from urllib.parse import urlsplit
import os
ROOT=Path(__file__).resolve().parents[1]/'pb_public'
BROWSER=os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH')
def synthetic_token(exp=4070995200):
 def segment(value):return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
 return segment({'alg':'HS256','typ':'JWT'})+'.'+segment({'id':'synthetic','exp':exp,'type':'auth','collectionId':'_pb_users_auth_'})+'.synthetic-signature'
AUTH={'token':synthetic_token(),'record':{'id':'synthetic','collectionName':'users','email':'demo@example.com','name':'Synthetic Visitor','verified':True}}
OAUTH_BROWSER_SCRIPT='''class FakeEventSource { constructor(){window.fakeStream=this;this.listeners={};setTimeout(()=>this.listeners.PB_CONNECT?.({data:JSON.stringify({clientId:'synthetic-state'})}),20);}addEventListener(n,cb){this.listeners[n]=cb;}close(){}}window.EventSource=FakeEventSource;window.open=()=>({closed:true,close(){},location:{set href(value){const stream=window.fakeStream;setTimeout(()=>stream.listeners['@oauth2']({data:JSON.stringify({state:'synthetic-state',code:'synthetic-code'})}),1000);}}});'''
for document,assets in [('index.html',('styles.css','app.js')),('terms/index.html',('styles.css',)),('privacy/index.html',('styles.css',))]:
 markup=(ROOT/document).read_text()
 for asset in assets:
  digest=hashlib.sha256((ROOT/asset).read_bytes()).hexdigest()[:12]
  assert f'{asset}?v={digest}' in markup,f'Stale asset hash: {document}: {asset}'
 if document=='index.html':assert '<title>Try VaultContext · Public demo</title>' in markup
 assert '/demo/' not in markup, f'Obsolete page path in {document}'
with sync_playwright() as p:
 b=p.chromium.launch(**({'executable_path':BROWSER} if BROWSER else {}))
 page=b.new_page(viewport={'width':390,'height':844})
 page.goto((ROOT/'index.html').as_uri())
 assert page.locator('.continue-button').is_disabled(), 'Preview must explain sign-in before enabling continuation'
 for choice in ('terms','commercial','newsletter'):
  assert page.locator('#'+choice).is_visible() and page.locator('#'+choice).is_disabled(), 'Choices stay visible but require sign-in'
 page.locator('#google-button').click()
 assert page.locator('.continue-button').is_enabled()
 page.locator('#terms').uncheck();assert page.locator('.continue-button').is_enabled()
 page.locator('.continue-button').focus();page.keyboard.press('Enter')
 assert page.locator('#terms').evaluate('(el)=>el === document.activeElement && el.validity.valueMissing && !!el.validationMessage')
 assert not page.locator('#onboarding').is_visible(), 'Unchecked terms must not open preview onboarding'
 page.locator('#terms').check();page.locator('.continue-button').click()
 assert page.locator('#onboarding').is_visible()
 assert not page.locator('#commercial').is_checked() and not page.locator('#newsletter').is_checked()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert page.locator('#agent-note').is_visible()
 assert page.locator('[data-method]').count()==0
 for d in page.locator('.guide-steps details').all():d.evaluate('(el)=>el.open=true')
 for c in page.locator('.step-done').all():c.check()
 assert page.locator('#guide-complete').is_visible();page.locator('#reset-progress').click();assert not page.locator('#guide-complete').is_visible()
 page.locator('a[href="#privacy-details"]').click();assert page.locator('#privacy-details').get_attribute('open') is not None
 page.close()
 page=b.new_page(viewport={'width':1440,'height':1000});calls=[];errors=[];pending_enroll=[];pending_preferences=[];hold_preferences=True;fail_preferences=False;client_ready=False;installation=None
 page.on('pageerror',lambda error:errors.append(str(error)))
 def route(r):
  path=urlsplit(r.request.url).path;calls.append((path,r.request.post_data))
  def result(x,status=200):r.fulfill(status=status,content_type='application/json',body=json.dumps(x))
  if path=='/api/demo/status':return result({'enabled':True,'generation':'2099-01-01','resetAt':'2099-01-02T00:00:00Z','termsVersion':'v1','clientReady':client_ready,'installation':installation})
  if path=='/api/collections/users/auth-methods':return result({'oauth2':{'providers':[{'name':'google','authURL':'https://accounts.google.com/o/oauth2/auth?client_id=synthetic&redirect_uri=','codeVerifier':'synthetic-verifier'}]}})
  if path=='/api/realtime':return r.fulfill(status=204)
  if path=='/api/collections/users/auth-with-oauth2':return result(AUTH)
  if path=='/api/collections/users/auth-refresh':return result(AUTH)
  if path=='/api/demo/preferences':
   if hold_preferences:pending_preferences.append(r);return
   if fail_preferences:return result({'message':'resetting'},503)
   return result({'revision':0,'salesContact':False,'newsletter':False})
  if path=='/api/demo/enroll':
   body=json.loads(r.request.post_data);assert body['salesConsent']==False and body['newsletterConsent']==False and body['expectedRevision'] in (0,1)
   pending_enroll.append(r);return
  if path=='/api/demo/unsubscribe':return result({'ok':True})
  target=ROOT/('index.html' if path=='/' else path.lstrip('/'))
  if target.is_dir():target=target/'index.html'
  if target.is_file():return r.fulfill(path=str(target))
  r.fulfill(status=404,body='Not found')
 page.route('**/*',route)
 page.add_init_script(OAUTH_BROWSER_SCRIPT)
 page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 assert page.locator('.continue-button').is_disabled()
 for choice in ('terms','commercial','newsletter'):assert page.locator('#'+choice).is_disabled()
 # A COOP-severed window handle reports closed before the independent callback.
 page.locator('#google-button').click()
 for choice in ('terms','commercial','newsletter'):assert page.locator('#'+choice).is_disabled(), 'OAuth must freeze choices'
 page.wait_for_function('document.querySelector("#form-status").textContent.includes("Google")')
 page.wait_for_timeout(1200)
 assert pending_preferences, 'Sign-in must fetch saved choices'
 for choice in ('terms','commercial','newsletter'):assert page.locator('#'+choice).is_disabled(), 'Preference loading must freeze choices'
 assert page.locator('#onboarding').is_hidden()
 hold_preferences=False
 pending_preferences.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'revision':0,'salesContact':False,'newsletter':False}))
 page.wait_for_selector('#identity:visible');assert 'Synthetic Visitor' in page.locator('#identity').inner_text()
 assert page.locator('.enrollment-card h3').inner_text()=='Review your choices'
 assert 'Sign in' not in page.locator('.card-intro').inner_text()
 assert page.locator('#google-note').is_hidden()
 assert page.locator('.continue-button').inner_text().strip()=='Accept terms & show instructions'
 assert page.title()=='Try VaultContext · Public demo'
 assert page.locator('.continue-button').is_enabled(), 'Ready visitors can submit to discover required terms'
 # Signed-in account text must stay in its own column, including long identities.
 assert page.locator('#identity-email').inner_text()=='demo@example.com'
 assert page.locator('.avatar').inner_text()=='S'
 assert 'synthetic identity' not in page.locator('#identity').inner_text()
 for width in (1440,390,320):
  page.set_viewport_size({'width':width,'height':1000})
  page.evaluate("setIdentity('long.synthetic.email.address.for.layout.checks@example.com', 'SyntheticVisitorWithAnUnusuallyLongUnbrokenDisplayName')")
  assert page.locator('#identity-email').inner_text()=='long.synthetic.email.address.for.layout.checks@example.com'
  assert page.evaluate("""() => {
   const card=document.querySelector('#identity'), avatar=card.querySelector('.avatar'), details=card.querySelector('.identity-details'), check=card.querySelector('.identity-check');
   const a=avatar.getBoundingClientRect(), d=details.getBoundingClientRect(), c=check.getBoundingClientRect(), bounds=card.getBoundingClientRect();
   return a.right<=d.left && d.right<=c.left && c.right<=bounds.right &&
     avatar.scrollWidth<=avatar.clientWidth && details.scrollWidth<=details.clientWidth &&
     card.scrollWidth<=card.clientWidth && document.documentElement.scrollWidth<=innerWidth;
  }"""), f'Identity overlaps or overflows at {width}px'
 page.set_viewport_size({'width':1440,'height':1000})
 page.evaluate("setIdentity('demo@example.com', 'Synthetic Visitor')")
 page.locator('#terms').check();assert page.locator('.continue-button').is_enabled()
 page.locator('#terms').uncheck();assert page.locator('.continue-button').is_enabled()
 enrollment_count=sum(path=='/api/demo/enroll' for path,_ in calls)
 page.locator('.continue-button').click()
 assert page.locator('#terms').evaluate('(el)=>el === document.activeElement && el.validity.valueMissing && !!el.validationMessage')
 assert not pending_enroll and sum(path=='/api/demo/enroll' for path,_ in calls)==enrollment_count
 # The explicit submit guard also protects synthetic events that skip browser validation.
 page.locator('#enrollment-form').evaluate('(form)=>form.dispatchEvent(new Event("submit", {bubbles:true, cancelable:true}))')
 page.wait_for_timeout(100)
 assert not pending_enroll and sum(path=='/api/demo/enroll' for path,_ in calls)==enrollment_count
 assert not page.locator('#commercial').is_checked() and not page.locator('#newsletter').is_checked()
 page.locator('#terms').check();page.locator('.continue-button').click()
 page.wait_for_function('document.querySelector("#signout").disabled')
 assert pending_enroll
 for choice in ('terms','commercial','newsletter'):assert page.locator('#'+choice).is_disabled(), 'Pending enrollment must freeze consent'
 assert page.locator('.continue-button').is_disabled(), 'Pending enrollment must not offer another submission'
 page.locator('#signout').evaluate('(button)=>button.click()')
 assert page.locator('#identity').is_visible(), 'Signout must not race a pending enrollment'
 pending_enroll.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'enrolled':True,'generation':'2099-01-01','contact':{'revision':1,'unsubscribeToken':'synthetic_withdrawal_'+'x'*24}}))
 page.wait_for_selector('#onboarding:visible')
 assert page.locator('#contact-permissions').is_visible() and page.locator('#contact-permissions').get_attribute('open') is None
 assert page.locator('#withdrawal-link').get_attribute('href')=='http://demo.test/#unsubscribe=synthetic_withdrawal_'+('x'*24)
 assert not any(path=='/api/demo/unsubscribe' for path,_ in calls), 'Saving or rendering a withdrawal link must not invoke it'
 assert 'example.invalid' in page.locator('#connect-code').text_content();assert 'distribution pending' in page.locator('#guide-intro').inner_text();assert 'Demo enrollment saved' in page.locator('#form-status').inner_text()
 stored=page.evaluate("JSON.parse(localStorage.getItem('pocketbase_auth'))")
 assert stored['token']==AUTH['token'] and stored['record']['id']=='synthetic'
 assert page.evaluate('Object.keys(localStorage)')==['pocketbase_auth'], 'Only browser authentication may persist'
 saved_withdrawal_link=page.locator('#withdrawal-link').get_attribute('href')
 fail_preferences=True
 page.get_by_text('Manage contact permissions',exact=True).click()
 page.locator('#withdraw').click();page.wait_for_function('document.querySelector("#form-status").textContent.includes("withdrawn")')
 page.wait_for_function('document.querySelector("#preferences-status").textContent.includes("Withdrawal succeeded")')
 page.evaluate("live.resetAt = new Date(Date.now() - 1000).toISOString(); updateCountdown();")
 assert page.locator('#contact-permissions').is_hidden() and page.locator('#withdrawal-link').get_attribute('href') is None
 assert page.locator('.continue-button').is_disabled(), 'Reset-due demo must not offer enrollment'
 assert not page.locator('#commercial').is_checked() and not page.locator('#newsletter').is_checked()
 assert not page.locator('#identity').is_visible(), 'Daily reset clears browser identity'
 assert page.locator('#google-button').is_visible()
 assert page.locator('.continue-button').is_disabled()
 assert not errors,errors
 assert any(x[0]=='/api/demo/enroll' for x in calls)
 # Failed preference loading cannot turn successful Google authentication into enrollment.
 page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 page.locator('#google-button').click()
 page.wait_for_function('!document.querySelector("#google-button").disabled && document.querySelector("#form-status").textContent.includes("unavailable")')
 assert page.locator('.continue-button').is_disabled()
 assert not page.locator('#identity').is_visible()
 # Backend readiness alone cannot bypass the explicit installation contract.
 client_ready=True;fail_preferences=False
 page.evaluate('localStorage.clear()')
 page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 install=page.locator('#install-code').text_content()
 assert 'unavailable' in install
 assert 'github.com' not in install and 'uv tool install' not in install
 page.locator('#google-button').click();page.wait_for_selector('#identity:visible');page.locator('#terms').check();page.locator('.continue-button').click()
 page.wait_for_function('document.querySelector("#signout").disabled')
 pending_enroll.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'enrolled':True,'generation':'2099-01-01','contact':{'revision':1,'unsubscribeToken':'synthetic_withdrawal_'+'x'*24}}))
 page.wait_for_selector('#onboarding:visible')
 assert 'example.invalid' in page.locator('#connect-code').text_content()
 assert 'unavailable' in page.locator('#install-code').text_content()
 assert 'npx skills add' not in page.locator('#install-code').text_content()
 assert not errors,errors
 # A preference-conflict refresh failure must invalidate the previously loaded revision.
 fail_preferences=True
 page.locator('.continue-button').click()
 page.wait_for_function('document.querySelector("#signout").disabled')
 pending_enroll.pop().fulfill(status=409,content_type='application/json',body='{}')
 page.wait_for_function('document.querySelector("#form-status").textContent.includes("Could not refresh preferences")')
 assert page.locator('.continue-button').is_disabled(), 'Stale preferences must not permit another enrollment'
 enrollment_count=sum(path=='/api/demo/enroll' for path,_ in calls)
 page.locator('#enrollment-form').evaluate('(form)=>form.requestSubmit()')
 page.wait_for_timeout(100)
 assert sum(path=='/api/demo/enroll' for path,_ in calls)==enrollment_count, 'A programmatic submit must also reject stale preferences'
 fail_preferences=False
 # The allowlisted public skill source and tested client revision enable setup.
 installation={'method':'skills','source':'https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext','skill':'vaultcontext','clientRevision':'78e0308abed01c21ffee5ec6b35a2d037c3f6280'}
 page.evaluate('localStorage.clear()')
 page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#install-code").textContent.includes("npx skills add")')
 assert page.locator('[data-method]').count()==0
 assert page.locator('#agent-note').get_attribute('hidden') is None
 install=page.locator('#install-code').text_content()
 assert 'https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext --skill vaultcontext --yes' in install
 assert 'private terminal' in install and 'same Google account' in install
 assert 'tar -xzf' not in install
 assert '\n\nnpx skills add' in install and r'\n' not in install
 page.locator('#google-button').click();page.wait_for_selector('#identity:visible');page.locator('#terms').check();page.locator('.continue-button').click()
 page.wait_for_function('document.querySelector("#signout").disabled')
 pending_enroll.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'enrolled':True,'generation':'2099-01-01','contact':{'revision':1,'unsubscribeToken':'synthetic_withdrawal_'+'x'*24}}))
 page.wait_for_selector('#onboarding:visible')
 assert "VAULTCONTEXT_URL='http://demo.test'" in page.locator('#connect-code').text_content()
 assert 'uv tool install' not in page.locator('#install-code').text_content()
 assert not any('/downloads/' in path for path,_ in calls)
 # A connected, ready demo must not show design-preview or future-launch wording,
 # including policy disclosures and the expanded onboarding instructions.
 for disclosure in page.locator('details').all():disclosure.evaluate('(element)=>element.open=true')
 visible_copy=page.locator('body').inner_text().lower()
 for obsolete in ('planned public demo', 'required to explore onboarding', 'proposed privacy', 'future demo', 'future daily reset', 'before any real enrollment', 'when the demo goes live', 'there is no live demo endpoint yet'):
  assert obsolete not in visible_copy, f'Live page still shows preview copy: {obsolete}'
 # Browser-native emoji rendering must not determine the appearance of action icons.
 for selector in ('.hero-actions .button', '.nav-cta', '.continue-button', '.features article:last-child .feature-symbol'):
  for element in page.locator(selector).all():
   assert element.locator('svg').count(), f'Missing scalable icon: {selector}'
   assert not any(char in element.text_content() for char in '↗↙'), f'Emoji-prone arrow: {selector}'
 for width in (390,320):
  page.set_viewport_size({'width':width,'height':844})
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  assert page.locator('.hero-actions .text-link').bounding_box()['height']>=44, 'Secondary action needs a mobile touch target'
  banner_lines=page.locator('#mode-banner > span:last-child').evaluate(r"""element=>{
   const lines=new Map(), walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){
    const node=walker.currentNode;
    for(const match of node.textContent.matchAll(/\S+/g)){
     const range=document.createRange();range.setStart(node,match.index);range.setEnd(node,match.index+match[0].length);
     const top=Math.round(range.getBoundingClientRect().top);lines.set(top,(lines.get(top)||0)+1);
    }
   }
   return [...lines.values()];
  }""")
  assert len(banner_lines)<2 or banner_lines[-1]>1, f'Banner ends with a single-word line at {width}px'
  headline=page.locator('h1').evaluate('(element)=>({height:element.getBoundingClientRect().height,line:parseFloat(getComputedStyle(element).lineHeight)})')
  assert headline['height']<=headline['line']*3+1, f'Headline wraps beyond three lines at {width}px'
 page.set_viewport_size({'width':1440,'height':1000})
 install=page.locator('#install-code').text_content()
 assert 'npx skills add https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext' in install
 assert 'tar -xzf' not in install
 assert '\n\nnpx skills add' in install and r'\n' not in install
 assert page.locator('#install-code').locator('xpath=../..').locator('.copy').inner_text()=='Copy instructions'
 # Copying sends the literal agent prompt only to the clipboard; failures stay visible.
 page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedInstructions=text}}})")
 page.locator('#install-code').locator('xpath=../..').locator('.copy').click()
 assert page.evaluate('window.copiedInstructions')==install
 assert 'No commands have been run' in page.locator('#copy-status').inner_text()
 page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('blocked')}}})")
 page.locator('#install-code').locator('xpath=../..').locator('.copy').click()
 assert 'Clipboard unavailable' in page.locator('#copy-status').inner_text()
 # Invalid installation contracts cannot inject a command, URL or revision.
 for field,value in [('method','shell'),('source','https://untrusted.example/skill'),('skill','other-skill'),('clientRevision','invalid'),('clientRevision','a'*40+';echo injected')]:
  previous=installation[field];installation[field]=value
  page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
  assert 'npx skills add' not in page.locator('#install-code').text_content()
  assert 'untrusted.example' not in page.content() and 'echo injected' not in page.content()
  installation[field]=previous
 # Readiness remains independently required even with a valid contract.
 client_ready=False
 page.goto('http://demo.test/');page.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 assert 'npx skills add' not in page.locator('#install-code').text_content()
 page.close()
 # A capability link remains useful after reset/account deletion. Merely opening
 # the page must never invoke the state-changing POST, and failures are retryable.
 for status_mode in ('resetting', 'offline'):
  page=b.new_page();capability=saved_withdrawal_link.split('#unsubscribe=',1)[1];withdrawals=[];attempts=[]
  def capability_route(r):
   path=urlsplit(r.request.url).path
   assert capability not in r.request.url
   assert capability not in r.request.headers.get('referer','')
   if path=='/api/demo/status':
    if status_mode=='offline':return r.abort('failed')
    return r.fulfill(status=503,content_type='application/json',body='{}')
   if path=='/api/demo/unsubscribe':
    assert r.request.method=='POST' and 'authorization' not in r.request.headers
    assert json.loads(r.request.post_data)=={'token':capability}
    attempts.append(1)
    if len(attempts)==1:return r.abort('failed')
    withdrawals.append(1);return r.fulfill(status=200,content_type='application/json',body='{"ok":true}')
   target=ROOT/('index.html' if path=='/' else path.lstrip('/'))
   if target.is_file():return r.fulfill(path=str(target))
   r.fulfill(status=404,body='Not found')
  page.route('**/*',capability_route)
  page.goto(saved_withdrawal_link)
  page.wait_for_selector('#link-withdrawal:visible')
  assert page.evaluate('location.hash')=='' and capability not in page.content()
  assert not attempts, 'GET/page load must never unsubscribe'
  assert not page.evaluate('localStorage.length || sessionStorage.length')
  page.locator('#confirm-withdrawal').click()
  page.wait_for_function('document.querySelector("#withdrawal-status").textContent.includes("could not be completed")')
  assert not page.locator('#confirm-withdrawal').is_disabled()
  page.locator('#confirm-withdrawal').click()
  page.wait_for_function('document.querySelector("#withdrawal-status").textContent.includes("request processed")')
  assert len(withdrawals)==1 and page.locator('#confirm-withdrawal').is_disabled()
  page.close()
 # Restored authentication must be checked by the server before it enables any
 # account UI. This context shares real LocalAuthStore storage between tabs.
 context=b.new_context();session_requests=[];refresh_status=200;reset_at='2099-01-02T00:00:00Z';refresh_pending=[];hold_refresh=False;hold_enrollment=False;session_enrollments=[]
 def session_route(r):
  path=urlsplit(r.request.url).path;session_requests.append(path)
  def result(data,status=200):r.fulfill(status=status,content_type='application/json',body=json.dumps(data))
  if path=='/api/demo/status':return result({'enabled':True,'generation':'2099-01-01','resetAt':reset_at,'termsVersion':'v1','clientReady':False})
  if path=='/api/collections/users/auth-refresh':
   assert r.request.method=='POST' and r.request.headers.get('authorization')==AUTH['token']
   if hold_refresh:refresh_pending.append(r);return
   return result(AUTH if refresh_status==200 else {'message':'Invalid session'},refresh_status)
  if path=='/api/demo/preferences':return result({'revision':0,'salesContact':False,'newsletter':False})
  if path=='/api/demo/enroll':
   if hold_enrollment:session_enrollments.append(r);return
   return result({'message':'Session expired'},401)
  target=ROOT/('index.html' if path=='/' else path.lstrip('/'))
  if target.is_file():return r.fulfill(path=str(target))
  return r.fulfill(status=404,body='Not found')
 context.route('**/*',session_route)
 first=context.new_page();first.goto('http://session.test/')
 first.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH)
 hold_refresh=True;first.reload()
 first.wait_for_timeout(150)
 assert refresh_pending, 'Reload must validate stored auth with authRefresh'
 for choice in ('terms','commercial','newsletter'):assert first.locator('#'+choice).is_disabled(), 'Session verification must freeze choices'
 assert first.locator('#identity').is_hidden() and first.locator('.continue-button').is_disabled(), 'Unvalidated localStorage must never imply authentication'
 refresh_pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(AUTH));hold_refresh=False
 first.wait_for_selector('#identity:visible')
 first.wait_for_function('!document.querySelector(".continue-button").disabled')
 assert first.locator('#identity-email').inner_text()=='demo@example.com'
 assert not first.locator('#onboarding').is_visible(), 'Restoring login must not fabricate enrollment'
 first.reload();first.wait_for_selector('#identity:visible')
 assert not first.locator('#terms').is_checked() and first.locator('.continue-button').is_enabled(), 'Restoring auth must not restore terms acceptance'
 assert not first.locator('#commercial').is_checked() and not first.locator('#newsletter').is_checked()
 second=context.new_page();second.goto('http://session.test/');second.wait_for_selector('#identity:visible')
 second.locator('#terms').check();second.wait_for_function('!document.querySelector(".continue-button").disabled')
 first.locator('#signout').click()
 second.wait_for_selector('#identity',state='hidden')
 assert second.locator('#google-button').is_visible() and second.locator('.continue-button').is_disabled()
 assert second.locator('#contact-permissions').is_hidden() and second.locator('#withdrawal-link').get_attribute('href') is None
 assert not second.locator('#onboarding').is_visible()
 assert not second.evaluate('JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token'), 'Signout must clear stored auth across tabs'
 second.close()
 # Invalid/deleted accounts clear the restored credential and expose sign-in.
 refresh_status=403;first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH);first.reload()
 first.wait_for_function('!JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 assert first.locator('#identity').is_hidden() and first.locator('#google-button').is_visible()
 assert first.locator('.continue-button').is_disabled()
 # Expired JWTs are rejected locally without forwarding stale credentials.
 expired={**AUTH,'token':synthetic_token(1)};refreshes=session_requests.count('/api/collections/users/auth-refresh')
 first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',expired);first.reload()
 first.wait_for_function('!JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 assert session_requests.count('/api/collections/users/auth-refresh')==refreshes
 assert first.locator('#identity').is_hidden() and first.locator('.continue-button').is_disabled()
 # A server 401 after successful restoration clears misleading signed-in UI too.
 refresh_status=200;first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH);first.reload()
 first.wait_for_selector('#identity:visible');first.locator('#terms').check();first.locator('.continue-button').click()
 first.wait_for_selector('#identity',state='hidden')
 assert first.locator('#google-button').is_visible() and first.locator('.continue-button').is_disabled()
 assert not first.evaluate('JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 # A refresh response arriving after another tab signs out must not resurrect
 # either the local identity or the shared persisted credential.
 first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH);first.reload()
 first.wait_for_selector('#identity:visible')
 hold_refresh=True;second=context.new_page();second.goto('http://session.test/')
 second.wait_for_timeout(150);assert refresh_pending
 first.locator('#signout').click()
 second.wait_for_function('!JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 refresh_pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(AUTH));hold_refresh=False
 second.wait_for_timeout(150)
 assert second.locator('#identity').is_hidden() and second.locator('.continue-button').is_disabled()
 assert not second.evaluate('JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token'), 'Late auth refresh revived a signed-out session'
 second.close()
 # Cross-tab signout also invalidates a successful enrollment already in flight.
 first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH);first.reload()
 first.wait_for_selector('#identity:visible')
 second=context.new_page();second.goto('http://session.test/');second.wait_for_selector('#identity:visible')
 hold_enrollment=True;second.locator('#terms').check();second.locator('.continue-button').click()
 second.wait_for_timeout(150);assert session_enrollments
 first.locator('#signout').click();second.wait_for_selector('#identity',state='hidden')
 session_enrollments.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'enrolled':True,'generation':'2099-01-01','contact':{'revision':1,'unsubscribeToken':'synthetic_withdrawal_'+'x'*24}}));hold_enrollment=False
 second.wait_for_timeout(150)
 assert second.locator('#identity').is_hidden() and second.locator('#onboarding').is_hidden()
 assert second.locator('.continue-button').is_disabled()
 assert not second.evaluate('JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 second.close()
 # A persisted login cannot cross a reset deadline even if auth-refresh would
 # accept it. No enrollment request may escape, including a scripted submission.
 reset_at='2000-01-01T00:00:00Z';enrollments=session_requests.count('/api/demo/enroll')
 first.evaluate('(auth)=>localStorage.setItem("pocketbase_auth",JSON.stringify(auth))',AUTH);first.reload()
 first.wait_for_function('document.querySelector("#countdown-note").textContent.includes("Reset")')
 assert first.locator('#terms').is_disabled()
 first.locator('#enrollment-form').evaluate('(form)=>form.requestSubmit()');first.wait_for_timeout(100)
 assert first.locator('.continue-button').is_disabled() and session_requests.count('/api/demo/enroll')==enrollments
 assert not first.evaluate('JSON.parse(localStorage.getItem("pocketbase_auth") || "{}").token')
 context.close()
 # Browsers blocking storage may still use a memory-only session. No SDK or
 # storage exception may break sign-in, and reload must require sign-in again.
 context=b.new_context();blocked=context.new_page();blocked_errors=[]
 blocked.on('pageerror',lambda error:blocked_errors.append(str(error)))
 blocked.route('**/*',route)
 blocked.add_init_script(OAUTH_BROWSER_SCRIPT)
 blocked.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Storage blocked','SecurityError')}})")
 blocked.goto('http://blocked.test/');blocked.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 blocked.locator('#google-button').click();blocked.wait_for_selector('#identity:visible')
 blocked.locator('#terms').check();assert blocked.locator('.continue-button').is_enabled()
 blocked.reload();blocked.wait_for_function('document.querySelector("#mode-pill").textContent === "Daily reset"')
 assert blocked.locator('#identity').is_hidden() and blocked.locator('#google-button').is_visible()
 assert blocked.locator('.continue-button').is_disabled() and not blocked_errors,blocked_errors
 context.close()
 print('PASS: static mobile preview/terms/optional consent/progress; live Google mock SSE/204/auth; preference revision enrollment; pending-release guide; withdrawal; signout; authentication-only localStorage; no page errors; pending-enrollment signout race; withdrawal across reset; fragment scrubbing/explicit confirmation/retry without login; sole coding-agent skill installation; invalid installation contracts rejected; continuation state and failed preference refresh; live copy and SVG icons; compact mobile headline/banner/touch targets; persisted session server validation; cross-tab signout; invalid sessions and reset clear auth; blocked storage fallback; late refresh/enrollment after cross-tab signout.')
 b.close()
