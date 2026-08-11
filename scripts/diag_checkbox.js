const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  await page.goto('http://localhost:8002/', { waitUntil: 'networkidle' });
  await page.evaluate(() => { if (typeof openModelModal === 'function') openModelModal(null); });
  await page.waitForSelector('#modelModalMask.show', { timeout: 5000 });
  await page.waitForTimeout(300);

  const get = (id) => page.$eval('#' + id, el => el.checked);

  // 真实点击视觉抽取复选框（冒泡）
  await page.click('#mdRoleVision');
  const vision1 = await get('mdRoleVision');
  await page.click('#mdRoleVision');
  const vision2 = await get('mdRoleVision');

  // 点 label 文字切换 结构化抽取
  const extractBefore = await get('mdRoleExtract');
  await page.click('label.chk:has(#mdRoleExtract)');
  const extractAfter = await get('mdRoleExtract');

  // 截图留存
  await page.screenshot({ path: 'D:/Clow/projects/wecom-archive-agent/data/_diag_modal_fixed.png' });

  console.log('视觉抽取 点击1(应true):', vision1, '| 点击2(应false):', vision2);
  console.log('结构化抽取 label文字点击 前:', extractBefore, '-> 后(应相反):', extractAfter);
  console.log('pageerrors:', errs.length ? errs.join('; ') : '无');
  await browser.close();
})().catch(e => { console.error('SCRIPT ERROR:', e); process.exit(1); });
