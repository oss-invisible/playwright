importScripts('/ab/realm.js' + location.search);
onconnect = e => {
  const port = e.ports[0];
  port.onmessage = async () => { port.postMessage(await abRealm()); };
};
