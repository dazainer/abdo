/// <reference lib="webworker" />
// Abdo's service worker: precaches the app shell (so the installed app opens
// instantly), serves index.html for client-side routes, and shows push
// notifications. /api is never cached: it always goes to the network.
import { cleanupOutdatedCaches, createHandlerBoundToURL, precacheAndRoute } from "workbox-precaching";
import type { PrecacheEntry } from "workbox-precaching";
import { NavigationRoute, registerRoute } from "workbox-routing";

declare let self: ServiceWorkerGlobalScope & { __WB_MANIFEST: Array<PrecacheEntry | string> };

precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();
registerRoute(
  new NavigationRoute(createHandlerBoundToURL("/index.html"), {
    denylist: [/^\/api\//, /^\/tg\//],
  }),
);

// A new deploy takes over right away instead of waiting for every tab to close.
self.addEventListener("install", () => {
  void self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

interface PushPayload {
  title?: string;
  body?: string;
  url?: string;
  silent?: boolean; // Fajr: shown without sound or vibration
  urgent?: boolean; // an urgent reminder: stays on screen, vibrates, has a Done button
  ack?: number | null; // the reminder a tap acknowledges (stops its repeats)
}

// Options some browsers support beyond the typed set (Android Chrome honours them; iOS ignores them).
type RichOptions = NotificationOptions & {
  vibrate?: number[];
  renotify?: boolean;
  requireInteraction?: boolean;
  actions?: { action: string; title: string }[];
};

async function ackReminder(id: number): Promise<void> {
  try {
    await fetch(`/api/reminders/${id}/ack`, { method: "POST", headers: { "X-Abdo": "1" }, credentials: "include" });
  } catch {
    /* offline: the next message to Abdo acknowledges it anyway */
  }
}

self.addEventListener("push", (event) => {
  let data: PushPayload = {};
  try {
    data = (event.data?.json() as PushPayload) ?? {};
  } catch {
    data = { body: event.data?.text() };
  }
  event.waitUntil(
    self.registration.showNotification(data.title ?? "Abdo", {
      body: data.body ?? "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      silent: data.silent ?? false,
      data: { url: data.url ?? "/", ack: data.ack ?? null },
      ...(data.urgent ? {
        requireInteraction: true,
        vibrate: [400, 150, 400, 150, 800],
        tag: data.ack ? `reminder-${data.ack}` : undefined,
        renotify: true,
        actions: [{ action: "done", title: "Done" }],
      } : {}),
    } as RichOptions),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url: string = event.notification.data?.url ?? "/";
  const ack: number | null = event.notification.data?.ack ?? null;
  event.waitUntil(
    (async () => {
      if (ack) await ackReminder(ack);
      if (event.action === "done") return; // "Done" stops the repeats without opening the app
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        await client.focus();
        await client.navigate(url);
        return;
      }
      await self.clients.openWindow(url);
    })(),
  );
});
