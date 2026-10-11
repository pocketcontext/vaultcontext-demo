// Disposable public-demo policy. Production is unchanged unless explicitly enabled.
const TERMS = 'demo-2026-10-09';
const LIMITS = {actions:500,accounts:1000,vaults:3,documents:20,versions:100,encodedBytes:33554432};
function enabled() {
 const value=$os.getenv('VAULTCONTEXT_DEMO_MODE') || '';
 if(!['','false','true'].includes(value))throw new Error('Invalid VAULTCONTEXT_DEMO_MODE');
 return value==='true';
}
function generation() {
 const value=$os.getenv('VAULTCONTEXT_DEMO_GENERATION') || '';
 if(!/^\d{4}-\d{2}-\d{2}$/.test(value) || new Date(value+'T00:00:00Z').toISOString().slice(0,10)!==value)
  throw new Error('A valid demo UTC generation is required');
 return value;
}
function current(app) {
 const day=generation(), policy=app.findRecordById('demo_policy','demopolicy00001');
 if(!policy.getBool('enabled') || policy.getString('generation')!==day || new Date().toISOString().slice(0,10)!==day)
  throw new ApiError(503,'The demo is resetting. Sign in again after reset.',{});
 return day;
}
function contacts(path,body) {
 const url=$os.getenv('VAULTCONTEXT_DEMO_CONTACT_URL') || '';
 const token=$os.getenv('VAULTCONTEXT_DEMO_CONTACT_TOKEN') || '';
 if(!/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(url) || token.length<32)
  throw new Error('Private demo contact service configuration is required');
 let response;
 try { response=$http.send({url:url+path,method:body===undefined?'GET':'POST',
  headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},
  body:body===undefined?undefined:JSON.stringify(body),timeout:5}); }
 catch(_){throw new ApiError(503,'Consent service unavailable; retry later.',{});}
 if(response.statusCode===409)throw new ApiError(409,'Preferences changed; refresh and try again.',{});
 if(response.statusCode===404 && body===undefined)return {revision:0,salesContact:false,newsletter:false};
 if(response.statusCode<200 || response.statusCode>=300)throw new ApiError(503,'Consent service unavailable; retry later.',{});
 return response.json;
}
function storageBoundary(app) {
 const fields=['BUCKET','ENDPOINT','REGION','ACCESS_KEY_ID','SECRET_ACCESS_KEY'];
 const prefixes=['VAULTCONTEXT_S3_','LITESTREAM_'];
 const remote=app.settings().s3.enabled || prefixes.some(prefix=>fields.some(field=>$os.getenv(prefix+field)));
 if(!remote)return; // Direct pinned-server local fixtures use isolated disk storage.
 for(const prefix of prefixes) {
  const required=prefix==='LITESTREAM_'?['BUCKET','ENDPOINT','REGION']:fields;
  if(required.some(field=>!$os.getenv(prefix+field)))throw new Error('Demo requires complete dedicated primary and replica storage configuration');
  const chosen=prefix==='VAULTCONTEXT_S3_'?'once-v2-vaultcontext-demo-files':'once-v2-vaultcontext-demo-replica';
  if($os.getenv(prefix+'BUCKET')!==chosen && !/^vaultcontext-demo-[a-z0-9][a-z0-9-]{1,40}$/.test($os.getenv(prefix+'BUCKET')))
   throw new Error('Demo storage requires dedicated vaultcontext-demo buckets');
 }
 if($os.getenv('VAULTCONTEXT_S3_BUCKET')===$os.getenv('LITESTREAM_BUCKET') ||
    ($os.getenv('LITESTREAM_ACCESS_KEY_ID') && $os.getenv('VAULTCONTEXT_S3_ACCESS_KEY_ID')===$os.getenv('LITESTREAM_ACCESS_KEY_ID')))
  throw new Error('Demo primary and replica storage require separate buckets and credentials');
 if(app.settings().s3.enabled && app.settings().s3.bucket!==$os.getenv('VAULTCONTEXT_S3_BUCKET'))
  throw new Error('Stored demo storage differs from its dedicated configuration');
}
function bootstrap(app) {
 const active=enabled();
 let policy;try{policy=app.findRecordById('demo_policy','demopolicy00001');}catch(_){policy=null;}
 if(!active) {
  if(policy && policy.getBool('enabled'))throw new Error('A demo database cannot run in production mode');
  return;
 }
 const day=generation();
 storageBoundary(app);
 if(policy && !policy.getBool('enabled')) {
  if(app.countRecords('users') || app.countRecords('vaults'))throw new Error('Demo requires a fresh isolated database');
  policy.set('enabled',true);policy.set('generation',day);app.save(policy);
 }
 if(policy)current(app);
 if(app.store().get('pocketcontextMaintenanceReadOnly')===true)return;
 // Passwords, OTP and email mutations must not offer alternate Google admission.
 const users=app.findCollectionByNameOrId('users');
 users.passwordAuth.enabled=false;users.otp.enabled=false;app.save(users);
 // A public demo must never silently start without abuse protection.
 const settings=app.settings();settings.rateLimits.enabled=true;
 settings.rateLimits.rules=require(`${__hooks}/deploy.js`).RULES;app.save(settings);
 const url=$os.getenv('VAULTCONTEXT_DEMO_CONTACT_URL') || '';
 const token=$os.getenv('VAULTCONTEXT_DEMO_CONTACT_TOKEN') || '';
 if(!/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(url) || token.length<32)
  throw new Error('Private demo contact service configuration is required');
}
function checkConfig() {
 if(!enabled())return;
 let path='pocketcontext.json';
 const args=$os.args;
 for(let i=0;i<args.length;i++) {
  if(args[i]==='--contextConfig')path=args[++i];
  else if(args[i].startsWith('--contextConfig='))path=args[i].slice(16);
 }
 const config=JSON.parse(toString($os.readFile(path))), filters=config.snapshot && config.snapshot.filters;
 if(!filters || filters.user_directory!=='id = :requester OR NOT EXISTS (SELECT 1 FROM demo_policy WHERE enabled = 1)' ||
 filters.identities!=='account = :requester OR NOT EXISTS (SELECT 1 FROM demo_policy WHERE enabled = 1)' ||
 !config.snapshot.policyTables || JSON.stringify(config.snapshot.policyTables.demo_policy)!=='["enabled"]' ||
 config.tables.demo_policy || config.tables.demo_enrollments || (config.tables.users))
  throw new Error('Demo requires its private directory SQL policy');
}
function publicInstallation(app) {
 // The packaged repository launcher is the single source of the tested client pin.
 // Read only this fixed public source file; never execute it or inspect credentials.
 let result=false;
 try {
  const bytes=$os.readFile('skills/vaultcontext/vaultcontext');
  if(bytes.length>4096)throw new Error('launcher too large');
  const matches=toString(bytes).match(/vaultcontext-client @ git\+https:\/\/github\.com\/pocketcontext\/vaultcontext-demo\.git@([a-f0-9]{40})"/g);
  if(!matches || matches.length!==1)throw new Error('invalid client pin');
  const revision=matches[0].match(/@([a-f0-9]{40})"$/)[1];
  result={method:'skills',source:'https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext',skill:'vaultcontext',clientRevision:revision};
 } catch(_) { result=false; }
 return result;
}
// The token is generated by the local supervisor, never exposed in status.
// Check the transport peer as well: proxy-derived realIP is not authentication.
function healthProbeLog(e) {
 const token=$os.getenv('VAULTCONTEXT_DEMO_HEALTH_TOKEN') || '';
 if(enabled() && token.length>=32 &&
    /^(127\.0\.0\.1|\[::1\]):[0-9]+$/.test(e.request.remoteAddr) &&
    e.request.header.get('X-VaultContext-Health-Token')===token)
  return $apis.skipSuccessActivityLog().func(e);
 return e.next();
}
function status(e) {
 e.response.header().set('Cache-Control','no-store');
 if(!enabled())return e.json(200,{enabled:false});
 const day=current(e.app),installation=publicInstallation(e.app);
 return e.json(200,{enabled:true,clientReady:!!installation,installation:installation||null,generation:day,resetAt:new Date(Date.parse(day+'T00:00:00Z')+86400000).toISOString(),termsVersion:TERMS,consentVersion:TERMS,limits:LIMITS});
}
function actor(e) {
 if(!enabled())throw new NotFoundError('Demo unavailable.');
 current(e.app);
 if(!e.auth || e.auth.collection().name!=='users' || e.auth.getBool('disabled'))throw new UnauthorizedError('Sign in with Google first.');
 const account=e.app.findRecordById('users',e.auth.id);
 subject(e.app,account);
 return account;
}
function subject(app,account) {
 const links=app.findRecordsByFilter('_externalAuths','recordRef = {:a} && provider = \"google\"','',1,0,{a:account.id});
 if(!links.length)throw new ForbiddenError('Google demo identity required.');
 return links[0].getString('providerId');
}
function preferences(e) {
 const account=actor(e);e.response.header().set('Cache-Control','no-store');
 return e.json(200,contacts('/preferences?subject='+encodeURIComponent(subject(e.app,account))));
}
function enroll(e) {
 const account=actor(e),body=e.requestInfo().body,day=current(e.app);
 if(body.generation!==day || body.termsVersion!==TERMS || body.acceptTerms!==true ||
 typeof body.salesConsent!=='boolean' || typeof body.newsletterConsent!=='boolean' ||
 !Number.isInteger(body.expectedRevision) || body.expectedRevision<0)
  throw new BadRequestError('Accept the current demo terms and choose each optional consent explicitly.');
 // Contact consent and its request event commit together before vault admission.
 // A later app failure keeps that explicit preference decision; refresh its
 // revision before retrying. This event does not assert successful admission.
 const previous=e.app.findRecordsByFilter('demo_enrollments','account = {:a}','',1,0,{a:account.id});
 const decision={subject:subject(e.app,account),email:account.getString('email'),
  salesContact:body.salesConsent,newsletter:body.newsletterConsent,termsVersion:TERMS,consentVersion:TERMS,expectedRevision:body.expectedRevision};
 if(!previous.length)decision.security={event:'enrollment_request',ip:e.realIP(),timestamp:Math.floor(Date.now()/1000)};
 const contact=contacts('/enroll',decision);
 e.app.runInTransaction(app=>{
  current(app);
  const rows=app.findRecordsByFilter('demo_enrollments','account = {:a}','',1,0,{a:account.id});
  const row=rows.length?rows[0]:new Record(app.findCollectionByNameOrId('demo_enrollments'));
  row.set('account',account.id);row.set('generation',day);row.set('terms_version',TERMS);
  row.set('sales_consent',body.salesConsent);row.set('newsletter_consent',body.newsletterConsent);
  row.set('contact_revision',contact.revision);app.save(row);
 });
 e.response.header().set('Cache-Control','no-store');
 return e.json(200,{enrolled:true,generation:day,termsVersion:TERMS,contact});
}
function unsubscribe(e) {
 if(!enabled())throw new NotFoundError('Demo unavailable.');
 const body=e.requestInfo().body;
 if(typeof body.token!=='string' || body.token.length<32 || body.token.length>128 || /[^A-Za-z0-9_-]/.test(body.token))throw new BadRequestError('Invalid withdrawal token.');
 e.response.header().set('Cache-Control','no-store');
 return e.json(200,contacts('/unsubscribe',{token:body.token}));
}
function enrollment(app,account) {
 const day=current(app),rows=app.findRecordsByFilter('demo_enrollments','account = {:a}','',1,0,{a:account});
 if(!rows.length || rows[0].getString('generation')!==day || rows[0].getString('terms_version')!==TERMS)
  throw new ForbiddenError('Accept the demo terms before using a vault.');
 return rows[0];
}
function authorizeAction(app,account,op,p) {
 if(!enabled())return;
 const usage=enrollment(app,account);
 if(usage.getInt('action_count')>=LIMITS.actions)throw new ApiError(429,'Demo action quota reached.',{});
 usage.set('action_count',usage.getInt('action_count')+1);app.save(usage);
 if(['share','accept','revoke','rotate','identity_rewrap'].includes(op))throw new ForbiddenError('Sharing and passphrase changes are unavailable in this disposable demo.');
 if(op==='vault_create' && app.countRecords('vaults',$dbx.hashExp({owner:account}))>=LIMITS.vaults)
  throw new ApiError(429,'Demo vault quota reached.',{});
}
function reserveSave(app,account,vault,existing,bytes) {
 if(!enabled())return;
 const row=enrollment(app,account);
 // vault is a text field, so count via explicit bound joins rather than relation expansion.
 const totals=new DynamicModel({documents:0,versions:0});
 app.db().newQuery('SELECT (SELECT count(*) FROM documents d JOIN vaults v ON d.vault=v.id WHERE v.owner={:a}) AS documents, (SELECT count(*) FROM versions f JOIN vaults v ON f.vault=v.id WHERE v.owner={:a}) AS versions').bind({a:account}).one(totals);
 if((!existing && totals.documents>=LIMITS.documents) || totals.versions>=LIMITS.versions || row.getInt('encoded_bytes')+bytes>LIMITS.encodedBytes)
  throw new ApiError(429,'Demo storage quota reached.',{});
 row.set('encoded_bytes',row.getInt('encoded_bytes')+bytes);app.save(row);
}
function guard(e) {
 if(!enabled())return e.next();
 // Static explanation remains available while all demo API traffic fails closed.
 const path=e.request.url.path;
 if((path.startsWith('/api/') && path!=='/api/demo/unsubscribe') || path==='/up')current(e.app);
 if(e.auth && e.auth.collection().name==='users' && (path.startsWith('/api/context/') || path.startsWith('/api/files/') || path==='/api/realtime'))enrollment(e.app,e.auth.id);
 return e.next();
}
function rejectAlternativeAuth(e) {
 if(enabled())throw new ForbiddenError('The public demo uses Google sign-in only; account credential changes are unavailable.');
 return e.next();
}
function userCreate(e) {
 if(!enabled())return e.next();
 current(e.app);
 e.app.runInTransaction(app=>{
  if(app.countRecords('users')>=LIMITS.accounts)throw new ApiError(429,'Demo account quota reached.',{});
  const original=e.app;e.app=app;try{e.next();}finally{e.app=original;}
 });
}
module.exports={enabled,current,bootstrap,checkConfig,healthProbeLog,status,preferences,enroll,unsubscribe,authorizeAction,reserveSave,guard,rejectAlternativeAuth,userCreate};
