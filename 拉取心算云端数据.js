// 拉取「心算任务」云端数据（等价于在取数页点「拉取」→「导出原始 JSON」）。
//
// 用法：
//   node 拉取心算云端数据.js --passcode <取数口令>
//   node 拉取心算云端数据.js --passcode <口令> --out d:\somewhere\sessions.json
//
// 拉完再执行： python 导入心算云端数据.py
//
// 说明：走的是页面同款的官方 SDK（取数页在用的 pull_sessions 口令函数），
// 不需要登录，但必须带取数口令；口令只从命令行传入，不写死在文件里。

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ENDPOINT = 'https://mental-math-68567.app.workbuddy.host';
const PUBLISHABLE_KEY = 'wbpk_4p0ke2O5DqAo1VLwI3JpAC_C7fa2Z7GPL6i0eMaxqPdJn0dE9cIK8Tl';
const SDK_URL = 'https://cdn.jsdelivr.net/npm/@tencent-ai/workbuddy-cloud-sdk@dev/lib/index.global.js';
const RPC = 'pull_sessions';

function parseArgs(argv) {
  const args = { out: path.join(__dirname, '.workbuddy', 'tmp', 'mental_math_sessions.json') };
  for (let i = 2; i < argv.length; i += 1) {
    if (argv[i] === '--passcode') args.passcode = argv[++i];
    else if (argv[i] === '--out') args.out = argv[++i];
    else if (argv[i] === '--help' || argv[i] === '-h') args.help = true;
  }
  return args;
}

(async () => {
  const args = parseArgs(process.argv);
  if (args.help || !args.passcode) {
    console.log('用法: node 拉取心算云端数据.js --passcode <取数口令> [--out 文件路径]');
    process.exit(args.help ? 0 : 1);
  }

  console.log('下载云端 SDK …');
  const sdkCode = await (await fetch(SDK_URL)).text();

  const sandbox = {
    console, fetch, URL, URLSearchParams, Headers, Request, Response,
    TextEncoder, TextDecoder, AbortController, crypto: globalThis.crypto,
    setTimeout, clearTimeout, setInterval, clearInterval, process, Buffer,
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    navigator: { userAgent: 'node' },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(sdkCode, sandbox, { filename: 'cloudsdk.js' });

  const Cloud = sandbox.WorkBuddyCloud;
  if (!Cloud) throw new Error('SDK 未导出 WorkBuddyCloud');

  const cloud = Cloud.createWorkBuddyCloud({ endpoint: ENDPOINT, publishableKey: PUBLISHABLE_KEY });
  const { data, error } = await cloud.database.rpc(RPC, { passcode: args.passcode });
  if (error) throw new Error(error.message || '口令不对或服务异常');

  const rows = Array.isArray(data) ? data : [];
  fs.mkdirSync(path.dirname(args.out), { recursive: true });
  fs.writeFileSync(args.out, JSON.stringify(rows, null, 2), 'utf8');

  const trials = rows.reduce((sum, row) => sum + (row.n_trials || 0), 0);
  const subjects = new Set(rows.map((row) => row.subject || '匿名')).size;
  console.log(`拉到 ${rows.length} 次会话 / ${subjects} 个被试编号 / ${trials} 题`);
  console.log('已保存：', args.out);
  console.log('下一步： python 导入心算云端数据.py');
})().catch((err) => {
  console.error('失败：', err.message);
  console.error('口令区分大小写；链接必须带 sharecode。');
  process.exit(2);
});
