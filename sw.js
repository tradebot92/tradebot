// Service worker minimal : permet l'installation de l'appli. Les données restent toujours en direct.
self.addEventListener("install",()=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("fetch",e=>{});
