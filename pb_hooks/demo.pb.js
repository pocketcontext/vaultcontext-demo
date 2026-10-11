onBootstrap((e)=>{
 e.next();
 require(`${__hooks}/demo.js`).bootstrap(e.app);
 require(`${__hooks}/demo.js`).checkConfig();
});
routerUse(new Middleware((e)=>require(`${__hooks}/demo.js`).guard(e),-1018));
routerAdd('GET','/api/demo/status',(e)=>require(`${__hooks}/demo.js`).status(e),
 new Middleware((e)=>require(`${__hooks}/demo.js`).healthProbeLog(e)));
routerAdd('GET','/api/demo/preferences',(e)=>require(`${__hooks}/demo.js`).preferences(e));
routerAdd('POST','/api/demo/unsubscribe',(e)=>require(`${__hooks}/demo.js`).unsubscribe(e));
routerAdd('POST','/api/demo/enroll',(e)=>require(`${__hooks}/demo.js`).enroll(e));
onRecordCreateExecute((e)=>require(`${__hooks}/demo.js`).userCreate(e),'users');
onRecordAuthWithPasswordRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
onRecordAuthWithOTPRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
onRecordRequestPasswordResetRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
onRecordConfirmPasswordResetRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
onRecordRequestEmailChangeRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
onRecordConfirmEmailChangeRequest((e)=>require(`${__hooks}/demo.js`).rejectAlternativeAuth(e),'users');
