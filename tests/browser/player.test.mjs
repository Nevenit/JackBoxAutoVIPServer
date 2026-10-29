// The player page (/j) against a real relay on a free port, in system Chromium
// at phone size. jackbox.tv is never contacted: its requests get a stub page and
// are recorded, so the test can count every room load.
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHROMIUM = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
const NAME = 'A%26B%20%231%20AND%20M'; // "a&b #1 and more", uppercased, cut to 12, encoded
const room = (code, name = NAME) => `https://jackbox.tv/?code=${code}&name=${name}`;

let failures = 0;
function check(name, condition) {
  console.log(`${condition ? 'ok  ' : 'FAIL'} ${name}`);
  if (!condition) failures++;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// True once `predicate` holds, false if it still doesn't after `timeout` ms.
async function until(predicate, timeout = 3000) {
  const deadline = Date.now() + timeout;
  while (!(await predicate())) {
    if (Date.now() > deadline) return false;
    await sleep(50);
  }
  return true;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer().once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const relay = spawn(
  join(root, '.venv', 'bin', 'python'),
  ['-m', 'uvicorn', '--factory', 'relay.app:create_app',
   '--host', '127.0.0.1', '--port', String(port), '--log-level', 'warning'],
  { cwd: root, stdio: ['ignore', 'inherit', 'inherit'] },
);
const relayExited = new Promise((resolve) => relay.once('exit', resolve));

async function api(method, path, token, body) {
  const response = await fetch(base + path, {
    method,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: body && JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${method} ${path}: HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}
const newNight = () => api('POST', '/sessions');
const publish = (night, code) =>
  api('POST', `/sessions/${night.session_id}/publish`, night.publisher_token, { code });
const clearRoom = (night) =>
  api('DELETE', `/sessions/${night.session_id}/code`, night.publisher_token);
const endNight = (night) => api('DELETE', `/sessions/${night.session_id}`, night.publisher_token);
const streams = async (night) =>
  (await api('GET', `/sessions/${night.session_id}`, night.publisher_token)).subscribers;
const fragment = (night) => `#${night.session_id}.${night.subscriber_token}`;

function decodeQr(file) {
  try {
    return execFileSync('zbarimg', ['--quiet', '--raw', file], { encoding: 'utf8' }).trim();
  } catch {
    return null; // zbarimg exits non-zero when it finds no code
  }
}

const shots = mkdtempSync(join(tmpdir(), 'jbvip-player-'));
let browser;
try {
  const started = await until(async () => {
    if (relay.exitCode !== null) throw new Error('the relay exited during startup');
    return fetch(`${base}/`).then((r) => r.status === 200, () => false);
  }, 10000);
  if (!started) throw new Error(`the relay never answered GET / with 200 on port ${port}`);

  browser = await puppeteer.launch({
    executablePath: CHROMIUM,
    headless: true,
    args: ['--no-sandbox'],
    defaultViewport: { width: 390, height: 844 },
  });
  const page = await browser.newPage();
  page.on('pageerror', (error) => check(`no uncaught error in the page: ${error.message}`, false));
  const requested = []; // every URL the page asked for
  const jackbox = []; // the jackbox.tv ones, in order
  const evictions = []; // when the page subscribed to the night whose streams end at once
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const url = request.url();
    requested.push(url);
    if (url.startsWith('https://jackbox.tv/')) {
      jackbox.push(url);
      return request.respond({ status: 200, contentType: 'text/html', body: '<p>jackbox.tv stub</p>' });
    }
    if (url.endsWith('/sessions/evicted/subscribe')) {
      // A stream that ends as soon as it opens, as when newer phones keep evicting this one.
      evictions.push(Date.now());
      return request.respond({ status: 200, contentType: 'text/event-stream', body: ': keepalive\n\n' });
    }
    request.continue();
  });
  // A missing element reads as hidden or empty, so it fails a check instead of ending the run.
  const visible = (id, p = page) => p.$eval(`#${id}`, (el) => el.checkVisibility()).catch(() => false);
  const text = (id, p = page) => p.$eval(`#${id}`, (el) => el.textContent).catch(() => '');
  const frameSrc = (p = page) => p.$eval('#frame', (el) => el.src).catch(() => '');
  const setVisibility = (state, p = page) => p.evaluate((state) => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
    document.dispatchEvent(new Event('visibilitychange'));
  }, state);

  // 1. No fragment.
  await page.goto(`${base}/j`);
  check('no fragment: says to open it from the QR code', await until(async () =>
    (await visible('wait-panel')) && (await text('wait-text')).includes("QR code on the host's screen")));

  // 2. First visit: a name that needs encoding and runs long.
  const s1 = await newNight();
  await page.goto(`${base}/j${fragment(s1)}`);
  check('first visit: asks for a name', await until(() => visible('name-panel')));
  await page.type('#name-input', 'a&b #1 and more');
  await page.click('#name-save');
  check('the name is stored uppercase and cut to 12 characters',
    (await page.evaluate(() => localStorage.getItem('jbvip-name'))) === 'A&B #1 AND M');
  check('a name and no room: the waiting screen', await until(() => visible('wait-panel')));

  // 3. A code frames the room.
  await publish(s1, 'WXYZ');
  check('a code frames jackbox.tv with the room and the encoded name', await until(async () =>
    (await visible('frame')) && (await frameSrc()) === room('WXYZ')));
  check('the frame may keep the screen awake',
    (await page.$eval('#frame', (el) => el.getAttribute('allow'))).includes('screen-wake-lock'));
  check('the bar shows the room code', (await text('code')) === 'WXYZ');
  await until(() => jackbox.length >= 1);
  check('jackbox.tv was loaded once', jackbox.length === 1 && jackbox[0] === room('WXYZ'));

  // 4. Hidden closes the stream; visible reopens it and the relay re-sends WXYZ.
  await setVisibility('hidden');
  check('hidden: the stream is closed', await until(async () => (await streams(s1)) === 0));
  await setVisibility('visible');
  check('visible again: the stream is reopened', await until(async () => (await streams(s1)) === 1));
  await sleep(500); // time for the re-sent WXYZ to arrive and be ignored
  check('the re-sent code does not reload the game',
    jackbox.length === 1 && (await frameSrc()) === room('WXYZ'));

  // 5. A new code swaps the room.
  await publish(s1, 'ABCD');
  check('a new code swaps the room', await until(async () =>
    (await frameSrc()) === room('ABCD') && jackbox.length === 2));

  // 6. Room cleared.
  await clearRoom(s1);
  check('room cleared: back to waiting with the frame hidden', await until(async () =>
    (await visible('wait-panel')) && !(await visible('frame'))));

  // 7. Invite QR.
  await publish(s1, 'QRST');
  await until(async () => (await frameSrc()) === room('QRST'));
  await page.click('#invite-btn');
  const qr = await page.waitForSelector('#qr', { visible: true });
  const shot = join(shots, 'qr.png');
  await qr.screenshot({ path: shot });
  check("the Invite QR decodes to the page's own link", decodeQr(shot) === page.url());

  // 8. Reload game.
  const loads = jackbox.length;
  await page.click('#menu-btn');
  await page.click('#reload-btn');
  check('Reload game loads the same room once more', await until(() =>
    jackbox.length === loads + 1 && jackbox.at(-1) === room('QRST')));

  // 8b. The host quits while this phone is in another app, so its stream is closed and it
  // misses the clear. Coming back, it must still end up waiting, not in the closed room.
  await setVisibility('hidden');
  const asleep = await until(async () => (await streams(s1)) === 0);
  await clearRoom(s1);
  await setVisibility('visible');
  check('a clear missed while hidden: back to waiting on return', asleep && await until(async () =>
    (await visible('wait-panel')) && !(await visible('frame')) && (await frameSrc()) === ''));

  // 9. A re-scan that only changes the fragment.
  const s2 = await newNight();
  await page.evaluate((hash) => { location.hash = hash; }, fragment(s2));
  await publish(s2, 'MNOP');
  check('a new fragment follows the new night', await until(async () =>
    (await frameSrc()) === room('MNOP')));
  check("the old night's stream is closed", await until(async () => (await streams(s1)) === 0));
  await publish(s1, 'ZZZZ');
  await sleep(500); // time for ZZZZ to arrive, if anything still listened
  check("the old night's codes no longer reach the page", (await frameSrc()) === room('MNOP'));

  // 10. The night ends with a game in the frame.
  await endNight(s2);
  check('night ended: the bar says so within 3 s', await until(async () =>
    (await text('status')).includes('ended'), 3000));
  check('the game stays in the frame', (await visible('frame')) && (await frameSrc()) === room('MNOP'));

  // 11. Streams that keep ending must not reconnect every second, or more phones than the
  // relay holds would evict each other in a loop.
  await page.evaluate(() => { location.hash = '#evicted.token'; });
  await until(() => evictions.length >= 3, 6000);
  const [first, second, third] = evictions;
  check('a stream that keeps ending backs off (1 s, then 2 s)', third - second > 1.5 * (second - first));

  // 12. A live night with the wrong token.
  await page.evaluate((hash) => { location.hash = hash; }, `#${s1.session_id}.wrong-token`);
  check("a wrong token: says the link doesn't work", await until(async () =>
    (await visible('wait-panel')) && (await text('wait-text')).includes("link doesn't work")));

  // 13. A late joiner: a new phone, with no name stored, opens the link itself (not a
  // hashchange) after the room was published. Its own browser context, so its own storage.
  const s3 = await newNight();
  await publish(s3, 'LATE');
  const joiner = await (await browser.createBrowserContext()).newPage();
  joiner.on('pageerror', (error) => check(`no uncaught error in the joiner's page: ${error.message}`, false));
  const joinerRooms = []; // the joiner's jackbox.tv loads
  await joiner.setRequestInterception(true);
  joiner.on('request', (request) => {
    const url = request.url();
    requested.push(url);
    if (!url.startsWith('https://jackbox.tv/')) return request.continue();
    joinerRooms.push(url);
    return request.respond({ status: 200, contentType: 'text/html', body: '<p>jackbox.tv stub</p>' });
  });
  await joiner.goto(`${base}/j${fragment(s3)}`);
  check('a late joiner is asked for a name first', await until(() => visible('name-panel', joiner)));
  await joiner.type('#name-input', 'late joiner');
  await joiner.click('#name-save');
  check('after Join, the room published before the page opened is framed', await until(async () =>
    (await frameSrc(joiner)) === room('LATE', 'LATE%20JOINER')));
  await sleep(500); // time for a second load to show up, if there were one
  check('the late joiner loaded jackbox.tv exactly once', joinerRooms.length === 1);

  // 14. A rename mid-game, to a name toUpperCase lengthens (ß becomes SS) so the cut at 12
  // lands inside the emoji. Half an emoji would make encodeURIComponent throw.
  // Clicked through the DOM: a mouse click just after a panel opens over the jackbox.tv frame
  // can reach that cross-origin frame, before the browser's hit-testing has seen the panel.
  const tap = (id) => joiner.$eval(`#${id}`, (el) => el.click());
  await tap('menu-btn');
  await tap('rename-btn');
  await joiner.$eval('#name-input', (el) => { el.value = ''; });
  await joiner.type('#name-input', 'Großer Han😀');
  await tap('name-save');
  check('a name cut inside an emoji keeps none of it',
    (await joiner.evaluate(() => localStorage.getItem('jbvip-name'))) === 'GROSSER HAN');
  await sleep(500); // time for a reload to show up, if there were one
  check('a rename does not reload the running game',
    joinerRooms.length === 1 && (await frameSrc(joiner)) === room('LATE', 'LATE%20JOINER'));
  await publish(s3, 'NEXT');
  check('the new name applies from the next room', await until(async () =>
    (await frameSrc(joiner)) === room('NEXT', 'GROSSER%20HAN') && joinerRooms.length === 2));

  // 15. Hide QR on TV: the TV shows its QR until any phone hides it. Two phones are on one night
  // with their menus open when one hides it. A missing button reads as hidden, so each "gone"
  // check needs a sighting first, or it would pass before the button exists.
  const s4 = await newNight();
  const tvQrHidden = async () =>
    (await api('GET', `/sessions/${s4.session_id}`, s4.publisher_token)).qr_hidden;
  const showQr = () => api('POST', `/sessions/${s4.session_id}/qr/show`, s4.publisher_token);
  // Through the DOM, as in 14; a missing element fails the check after it instead of the run.
  const tapOn = (p, id) => p.$eval(`#${id}`, (el) => el.click()).catch(() => {});
  const phoneLoads = []; // the phones' jackbox.tv loads
  let failNextHide = false;
  // A joined phone in a browser context of its own: a second tab in one context hides the
  // first, and a hidden page closes its stream.
  const openPhone = async () => {
    const phone = await (await browser.createBrowserContext()).newPage();
    phone.on('pageerror', (error) => check(`no uncaught error in a phone's page: ${error.message}`, false));
    await phone.setRequestInterception(true);
    phone.on('request', (request) => {
      const url = request.url();
      requested.push(url);
      if (url.startsWith('https://jackbox.tv/')) {
        phoneLoads.push(url);
        return request.respond({ status: 200, contentType: 'text/html', body: '<p>jackbox.tv stub</p>' });
      }
      if (failNextHide && url.endsWith('/qr/hide')) {
        failNextHide = false;
        return request.respond({ status: 503, contentType: 'text/plain', body: 'down' });
      }
      request.continue();
    });
    await phone.goto(`${base}/j${fragment(s4)}`);
    await phone.type('#name-input', 'qr');
    await phone.click('#name-save');
    return phone;
  };
  const hider = await openPhone();
  const watcher = await openPhone();
  await publish(s4, 'HIDE');
  await until(async () => (await frameSrc(hider)) === room('HIDE', 'QR') &&
    (await frameSrc(watcher)) === room('HIDE', 'QR') && phoneLoads.length === 2);

  await tapOn(hider, 'menu-btn');
  await tapOn(watcher, 'menu-btn');
  const offered = await until(async () =>
    (await visible('qr-hide-btn', hider)) && (await visible('qr-hide-btn', watcher)));
  check('the menu offers Hide QR on TV while the TV QR is up',
    offered && (await text('qr-hide-btn', hider)) === 'Hide QR on TV');

  await tapOn(hider, 'qr-hide-btn');
  const hidOnRelay = await until(async () => (await tvQrHidden()) === true);
  check('Hide QR on TV hides it on the relay', hidOnRelay);
  const menuClosed = await until(async () => !(await visible('menu-panel', hider)));
  await tapOn(hider, 'menu-btn');
  check('the button is gone after hiding', offered && menuClosed &&
    (await visible('menu-panel', hider)) && !(await visible('qr-hide-btn', hider)));
  check('a second open phone loses the button too', offered && await until(async () =>
    (await visible('menu-panel', watcher)) && !(await visible('qr-hide-btn', watcher))));
  await sleep(500); // time for a reload to show up, if there were one
  check('hiding does not reload the running game', hidOnRelay && phoneLoads.length === 2 &&
    (await frameSrc(hider)) === room('HIDE', 'QR') && (await frameSrc(watcher)) === room('HIDE', 'QR'));

  await showQr();
  const back = await until(async () =>
    (await visible('qr-hide-btn', hider)) && (await visible('qr-hide-btn', watcher)));
  check('the host showing it again brings the button back', back);
  await watcher.evaluate(() => onEvent({ code: 'HIDE', ts: 1 })); // as from a relay without the flag
  check('an event without the QR flag counts as hidden', back && !(await visible('qr-hide-btn', watcher)));

  failNextHide = true;
  await tapOn(hider, 'qr-hide-btn');
  check('a failed hide says so and keeps the button', await until(async () =>
    (await text('qr-hide-btn', hider)) === "Couldn't hide it. Try again") &&
    (await visible('qr-hide-btn', hider)));
  await tapOn(hider, 'menu-close');
  await tapOn(hider, 'menu-btn');
  const relabelled = (await text('qr-hide-btn', hider)) === 'Hide QR on TV';
  await tapOn(hider, 'qr-hide-btn');
  check('reopening the menu resets the label, and a retry hides it',
    relabelled && await until(async () => (await tvQrHidden()) === true));

  await showQr();
  await tapOn(hider, 'menu-btn'); // the retry closed it
  const again = await until(() => visible('qr-hide-btn', hider));

  // The phone leaves with the QR up and comes back after another phone hid it. Until the first
  // event of its new stream it knows nothing about the QR, so it must not offer the button. A real
  // relay sends that event with the response headers, which would make this check a race; so the
  // new stream is held open with no event, which keeps the moment between the two for as long as
  // the check needs.
  await hider.evaluate(() => {
    const realFetch = window.fetch;
    window.fetch = (url, init) => {
      if (!window.holdSubscribe || !String(url).endsWith('/subscribe')) return realFetch(url, init);
      window.holdSubscribe = false;
      const body = new ReadableStream({
        start: (controller) =>
          init.signal.addEventListener('abort', () => controller.error(init.signal.reason)),
      });
      return Promise.resolve(new Response(body, {
        status: 200, headers: { 'content-type': 'text/event-stream' },
      }));
    };
    window.holdSubscribe = true;
  });
  await setVisibility('hidden', hider);
  await api('POST', `/sessions/${s4.session_id}/qr/hide`, s4.subscriber_token); // another phone
  await setVisibility('visible', hider);
  await sleep(300); // time for the button to show, if it were going to
  check('a phone coming back is not offered the button before the relay says the QR is up',
    again && !(await visible('qr-hide-btn', hider)));
  await setVisibility('hidden', hider); // ends the held stream
  await showQr();
  await setVisibility('visible', hider);
  check('once the relay says the QR is up, a returning phone is offered the button again',
    await until(() => visible('qr-hide-btn', hider)));

  await endNight(s4);
  check('a night that has ended offers no button', again && await until(async () =>
    (await visible('menu-panel', hider)) && !(await visible('qr-hide-btn', hider))));

  // 16. Admin password. A night registered with the key of "hunter2" and a 0.3 s head start.
  const KEY = 'd2f597d1c562c90fdcb4f02255abdf38aee33a342ed215663554309fb17091c6';
  const s5 = await newNight();
  await api('PUT', `/sessions/${s5.session_id}/admin`, s5.publisher_token, { key: KEY, head_start: 0.3 });
  const adminLoads = []; // [phone, url] for every jackbox.tv load
  const openAdminPhone = async (night) => {
    const phone = await (await browser.createBrowserContext()).newPage();
    phone.on('pageerror', (error) => check(`no uncaught error in an admin test page: ${error.message}`, false));
    await phone.setRequestInterception(true);
    phone.on('request', (request) => {
      const url = request.url();
      requested.push(url);
      if (!url.startsWith('https://jackbox.tv/')) return request.continue();
      adminLoads.push([phone, url]);
      return request.respond({ status: 200, contentType: 'text/html', body: '<p>jackbox.tv stub</p>' });
    });
    await phone.goto(`${base}/j${fragment(night)}`);
    await phone.type('#name-input', 'adm');
    await phone.click('#name-save');
    return phone;
  };
  const loadsOf = (phone) => adminLoads.filter(([p]) => p === phone).length;
  const tapA = (p, id) => p.$eval(`#${id}`, (el) => el.click()).catch(() => {});
  const signIn = async (p, password) => {
    if (!(await visible('menu-panel', p))) await tapA(p, 'menu-btn');
    await p.type('#admin-input', password);
    await tapA(p, 'admin-become');
  };
  const stored = (p, key) => p.evaluate((k) => localStorage.getItem(k), key);
  const isAdmin = (p) => visible('admin-mark', p);
  const adminRoom = (code) => room(code, 'ADM');

  const adm = await openAdminPhone(s5);
  const guest = await openAdminPhone(s5);
  await publish(s5, 'ONE1');
  await until(async () => (await frameSrc(adm)) === adminRoom('ONE1') && (await frameSrc(guest)) === adminRoom('ONE1'));
  check('a guest has no admin marker', !(await isAdmin(guest)));
  const before = loadsOf(adm);
  await signIn(adm, 'hunter2');
  check('the right password makes the bar show admin', await until(() => isAdmin(adm)));
  check('the derived key is stored, not the password',
    (await stored(adm, 'jbvip-admin')) === KEY &&
    !(await adm.evaluate(() => JSON.stringify(localStorage))).includes('hunter2'));
  await sleep(500);
  check('becoming admin does not reload the framed game', loadsOf(adm) === before);
  await adm.reload();
  check('a reload keeps the admin marker', await until(() => isAdmin(adm)));

  // The admin gets the next room at once, the guest after the head start.
  let admAt = 0, guestAt = 0;
  const stamp = async () => {
    if (!admAt && (await frameSrc(adm)) === adminRoom('TWO2')) admAt = Date.now();
    if (!guestAt && (await frameSrc(guest)) === adminRoom('TWO2')) guestAt = Date.now();
    return admAt && guestAt;
  };
  await publish(s5, 'TWO2');
  await until(stamp, 4000);
  check('the guest gets a new room about 0.2 s or more after the admin',
    admAt > 0 && guestAt > 0 && guestAt - admAt >= 150);

  // Priority off connects without the header.
  await tapA(adm, 'menu-btn');
  check('the menu says Priority: on', (await text('admin-priority', adm)) === 'Priority: on');
  await tapA(adm, 'admin-priority');
  check('Priority off drops the marker', await until(async () => !(await isAdmin(adm))));
  check('and says Priority: off', (await text('admin-priority', adm)) === 'Priority: off');
  check('and keeps the key',
    (await stored(adm, 'jbvip-admin')) === KEY && (await stored(adm, 'jbvip-priority')) === 'off');

  // Ruling 4: demoted mid-window, the page never steps back to the old room.
  await tapA(adm, 'admin-priority'); // priority on again
  await until(() => isAdmin(adm));
  const swaps = loadsOf(adm);
  await publish(s5, 'THR3');
  await until(async () => (await frameSrc(adm)) === adminRoom('THR3'));
  await tapA(adm, 'admin-priority'); // off inside the head-start window: seeded the old room
  await sleep(800); // the window ends and the guest catch-up arrives
  check('demoted mid-window, the frame never steps back to the older room',
    (await frameSrc(adm)) === adminRoom('THR3') && loadsOf(adm) === swaps + 1);

  // Stop being admin.
  await tapA(adm, 'admin-stop');
  check('Stop being admin clears both keys', await until(async () =>
    (await stored(adm, 'jbvip-admin')) === null && (await stored(adm, 'jbvip-priority')) === null));
  check('and the sign-in form is back', await until(() => visible('admin-input', adm)));

  // A wrong password.
  await signIn(adm, 'nope');
  check('a wrong password says so', await until(async () =>
    (await text('admin-note', adm)) === 'Wrong admin password'));
  check('and clears the stored key', (await stored(adm, 'jbvip-admin')) === null && !(await isAdmin(adm)));

  // No registration tonight.
  const s6 = await newNight();
  const bare = await openAdminPhone(s6);
  await signIn(bare, 'hunter2');
  check('with no registration, says No admin tonight', await until(async () =>
    (await text('admin-note', bare)) === 'No admin tonight'));
  check('and keeps the key', (await stored(bare, 'jbvip-admin')) === KEY);

  // Enter in the field submits, as the phone keyboard's Go does.
  const enter = await openAdminPhone(s5);
  await tapA(enter, 'menu-btn');
  await enter.type('#admin-input', 'hunter2');
  await enter.keyboard.press('Enter');
  check('Enter in the password field makes the page admin', await until(() => isAdmin(enter)));

  check('no request URL carries the admin key',
    requested.every((url) => !url.includes(KEY) && !url.includes('hunter2')));

  const tokens = [s1, s2, s3, s4, s5, s6].map((night) => night.subscriber_token);
  check('no request URL carries a token (the fragment is never sent)',
    requested.every((url) => !tokens.some((token) => url.split('#')[0].includes(token))));
} finally {
  await browser?.close().catch(() => {});
  relay.kill('SIGKILL');
  await relayExited;
  rmSync(shots, { recursive: true, force: true });
}

process.exit(failures === 0 ? 0 : 1);
