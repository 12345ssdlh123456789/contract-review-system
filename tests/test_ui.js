// 前端冒烟测试：用 playwright 真开浏览器，把四个角色的主要页面都点一遍
const path = require('path');
// 默认用本地安装的 playwright；装在别处时用 PLAYWRIGHT_MODULE 指定模块路径
const PW = process.env.PLAYWRIGHT_MODULE || '@playwright/test';
const { chromium } = require(PW);

const BASE = process.env.BASE_URL || 'http://127.0.0.1:3000';
// 截图默认落在 tests/shots（已 gitignore），可用 SHOT_DIR 覆盖
const OUT = process.env.SHOT_DIR || path.join(__dirname, 'shots');
const fs = require('fs');
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

const errors = [];
const results = [];

function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? '  ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text());
  });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('requestfailed', (r) => errors.push(`reqfail: ${r.url()} ${r.failure()?.errorText}`));

  // ---------- 登录页 ----------
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  check('登录页渲染', await page.isVisible('#screenLogin'));
  check('登录卡片标题', (await page.textContent('.login-brand h1')).includes('企业合同智能审查'));
  check('演示账号 4 个', (await page.locator('.demo-chip').count()) === 4);
  await page.screenshot({ path: path.join(OUT, '01_login.png') });

  // ---------- 空表单提交 ----------
  await page.click('#loginBtn');
  await page.waitForTimeout(250);
  check('空用户名提示', (await page.textContent('#loginErr')).includes('请输入用户名'));

  // ---------- 错误密码 ----------
  await page.fill('#loginUser', 'reviewer');
  await page.fill('#loginPass', 'wrongpass');
  await page.click('#loginBtn');
  await page.waitForTimeout(900);
  const errTxt = await page.textContent('#loginErr');
  check('错误密码被拒', errTxt.includes('不正确'), `提示="${errTxt}"`);
  await page.screenshot({ path: path.join(OUT, '02_login_err.png') });

  // ---------- 演示账号一键填充 ----------
  await page.click('.demo-chip[data-u="reviewer"]');
  await page.waitForTimeout(200);
  check('一键填充用户名', (await page.inputValue('#loginUser')) === 'reviewer');
  check('一键填充密码', (await page.inputValue('#loginPass')) === 'demo123456');

  // ---------- 登录成功 ----------
  await page.click('#loginBtn');
  await page.waitForSelector('#screenApp.active', { timeout: 15000 });
  await page.waitForTimeout(1200);
  check('登录后进入主界面', await page.isVisible('#screenApp'));
  check('显示用户角色', (await page.textContent('#userRole')) === '法务审查员');
  await page.screenshot({ path: path.join(OUT, '03_contracts.png') });

  // ---------- 合同列表 ----------
  const items = await page.locator('.contract-item').count();
  check('合同列表有数据', items > 0, `${items} 份`);

  // 打开第一份
  if (items > 0) {
    await page.locator('.contract-item').first().click();
    await page.waitForTimeout(1500);
    check('合同详情渲染', await page.isVisible('.detail-head'));
    const clauseCount = await page.locator('.clause').count();
    check('条款列表渲染', clauseCount > 0, `${clauseCount} 条`);
    const findingCount = await page.locator('.finding').count();
    check('审查意见渲染', findingCount > 0, `${findingCount} 条意见`);
    await page.screenshot({ path: path.join(OUT, '04_detail.png'), fullPage: true });
  }

  // ---------- 看板 ----------
  await page.click('.tab[data-view="dashboard"]');
  await page.waitForTimeout(1600);
  const statCards = await page.locator('.stat-card').count();
  check('看板统计卡', statCards === 7, `${statCards} 张`);
  check('看板有柱状条', (await page.locator('.bar-row').count()) > 0);
  await page.screenshot({ path: path.join(OUT, '05_dashboard.png'), fullPage: true });

  // ---------- 评测（reviewer 应该看不到） ----------
  const evalTabHidden = await page.locator('.tab[data-view="eval"]').evaluate((el) =>
    el.classList.contains('hidden')
  );
  check('reviewer 看不到评测 tab', evalTabHidden);

  // ---------- Prompt 版本（reviewer 能看） ----------
  await page.click('.tab[data-view="prompts"]');
  await page.waitForTimeout(1800);
  const promptCards = await page.locator('#promptBody .card').count();
  check('Prompt 版本卡片', promptCards >= 2, `${promptCards} 个`);
  check('Prompt 有生效标记', (await page.locator('#promptBody .pill-ok').count()) > 0);
  await page.screenshot({ path: path.join(OUT, '06_prompts.png'), fullPage: true });

  // ---------- 新建合同弹窗 ----------
  await page.click('.tab[data-view="contracts"]');
  await page.waitForTimeout(600);
  await page.click('#btnNewContract');
  await page.waitForTimeout(400);
  check('弹窗打开', await page.isVisible('#modalMask.show'));
  await page.click('.mtab[data-mode="text"]');
  await page.waitForTimeout(200);
  check('切到粘贴文本', await page.isVisible('#panelText.active'));
  await page.click('#modalSubmit');
  await page.waitForTimeout(300);
  check('空标题校验', (await page.textContent('#newErr')).includes('合同标题'));
  await page.screenshot({ path: path.join(OUT, '07_modal.png') });
  await page.click('#modalCancel');

  // ---------- 退出 + 换 admin ----------
  await page.click('#logoutBtn');
  await page.waitForTimeout(600);
  check('退出回到登录页', await page.isVisible('#screenLogin'));

  await page.fill('#loginUser', 'admin');
  await page.fill('#loginPass', 'demo123456');
  await page.click('#loginBtn');
  await page.waitForSelector('#screenApp.active', { timeout: 15000 });
  await page.waitForTimeout(1200);
  check('admin 登录', (await page.textContent('#userRole')) === '管理员');
  const evalVisible = await page.locator('.tab[data-view="eval"]').evaluate(
    (el) => !el.classList.contains('hidden')
  );
  check('admin 能看到评测 tab', evalVisible);

  // ---------- 评测页 ----------
  await page.click('.tab[data-view="eval"]');
  await page.waitForTimeout(2500);
  const metricCount = await page.locator('.metric').count();
  check('评测指标卡', metricCount === 4, `${metricCount} 张`);
  const runRows = await page.locator('#evalBody table.data tbody tr').count();
  check('评测历史表有数据', runRows > 0, `${runRows} 行`);
  await page.screenshot({ path: path.join(OUT, '08_eval.png'), fullPage: true });

  // ---------- 审计 ----------
  await page.click('.tab[data-view="audit"]');
  await page.waitForTimeout(1600);
  const auditRows = await page.locator('#auditBody table.data tbody tr').count();
  check('审计日志有数据', auditRows > 0, `${auditRows} 条`);
  await page.screenshot({ path: path.join(OUT, '09_audit.png'), fullPage: true });

  // ---------- 看板（admin） ----------
  await page.click('.tab[data-view="dashboard"]');
  await page.waitForTimeout(1600);
  await page.screenshot({ path: path.join(OUT, '10_dash_admin.png'), fullPage: true });

  // ---------- finance 权限隔离 ----------
  await page.click('#logoutBtn');
  await page.waitForTimeout(600);
  await page.fill('#loginUser', 'finance');
  await page.fill('#loginPass', 'demo123456');
  await page.click('#loginBtn');
  await page.waitForSelector('#screenApp.active', { timeout: 15000 });
  await page.waitForTimeout(1500);
  check('finance 登录', (await page.textContent('#userRole')) === '财务');
  check('finance 看不到审计 tab', await page.locator('.tab[data-view="audit"]').evaluate((el) =>
    el.classList.contains('hidden')
  ));

  await page.locator('.contract-item').first().click();
  await page.waitForTimeout(1500);
  const finClauses = await page.locator('.clause').count();
  check('finance 能看到部分条款', finClauses > 0, `${finClauses} 条`);
  const hiddenHint = await page.locator('#contractDetail .hint').first().textContent().catch(() => '');
  check('finance 有权限提示', hiddenHint.includes('不可见'), `提示="${(hiddenHint || '').slice(0, 40)}"`);
  await page.screenshot({ path: path.join(OUT, '11_finance.png'), fullPage: true });

  // ---------- 汇总 ----------
  console.log('\n================ 汇总 ================');
  const pass = results.filter((r) => r.ok).length;
  console.log(`${pass} 通过 / ${results.length - pass} 失败 / 共 ${results.length}`);
  if (errors.length) {
    console.log(`\n浏览器错误 ${errors.length} 条：`);
    [...new Set(errors)].slice(0, 20).forEach((e) => console.log('  ' + e));
  } else {
    console.log('无浏览器错误');
  }

  await browser.close();
  process.exit(pass === results.length && errors.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error('测试脚本异常:', e.message);
  process.exit(2);
});
