import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import puppeteer from 'puppeteer-core';

const here = dirname(fileURLToPath(import.meta.url));
const CHROMIUM = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
const inject = readFileSync(join(here, 'inject.js'), 'utf8');
// Built via pathToFileURL (not manual 'file://' + path) so paths containing
// characters with URL significance — this repo lives under .../C#/... — are
// percent-encoded instead of being misparsed (a bare '#' starts a fragment).
const FIXTURE = pathToFileURL(join(here, 'fixture.html')).href;

let failures = 0;
function check(name, condition) {
  console.log(`${condition ? 'ok  ' : 'FAIL'} ${name}`);
  if (!condition) failures++;
}

async function freshPage(browser, url) {
  const page = await browser.newPage();
  if (url) { await page.goto(url); } else { await page.setContent('<p>no form here</p>'); }
  await page.evaluate(inject);
  return page;
}

const browser = await puppeteer.launch({
  executablePath: CHROMIUM,
  headless: true,
  args: ['--no-sandbox'],
});

try {
  let page = await freshPage(browser, FIXTURE);
  let result = await page.evaluate(() => window.jbvipJoin('WEWW', 'Michael'));
  const joinedText = await page.$eval('#joined', (el) => el.textContent);
  check('reports success', result.ok === true);
  check('vue model received the code', joinedText.includes('WEWW'));
  check('vue model received the name', joinedText.includes('Michael'));

  page = await freshPage(browser, FIXTURE);
  result = await page.evaluate(() => window.jbvipJoin('FAIL', 'Michael'));
  check('rejected join reports failure', result.ok === false);
  check('rejected join surfaces the page message', /Room not found/.test(result.reason));

  page = await freshPage(browser, null);
  result = await page.evaluate(() => window.jbvipJoin('WEWW', 'Michael'));
  check('missing form is detected', result.reason === 'form-not-found');
} finally {
  await browser.close();
}

process.exit(failures === 0 ? 0 : 1);
