importScripts('/ab/realm.js' + location.search);
onmessage = async () => { postMessage(await abRealm()); };
