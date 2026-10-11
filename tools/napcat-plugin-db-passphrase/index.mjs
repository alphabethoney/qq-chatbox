// db-passphrase：NapCat 登录后打印并保存 NTQQ 数据库口令（16 字符 ASCII）。
// 该口令账号固定、多次登录不变，只需在「导出账号」登录时捕获一次，
// 之后即可用 qq-exporter --passphrase <口令> 离线解密导出。
//
// 安装：把本目录整个复制到 <NapCat>\napcat\plugins\db-passphrase\
//
// 说明：passphrase 只有在登录完成、数据库被打开后才存在，因此这里轮询等待。

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

function resolveDbApi(ctx) {
  const roots = [ctx, ctx.core, ctx.apis, ctx.core && ctx.core.apis];
  for (const r of roots) {
    if (!r) continue;
    if (typeof r.getPassphraseBuffer === 'function') return r;
    for (const key of ['DatabaseApi', 'apis', 'api']) {
      const sub = r[key];
      if (sub && typeof sub.getPassphraseBuffer === 'function') return sub;
      if (sub && sub.DatabaseApi && typeof sub.DatabaseApi.getPassphraseBuffer === 'function') {
        return sub.DatabaseApi;
      }
    }
  }
  return null;
}

export const plugin_init = async (ctx) => {
  const dbApi = resolveDbApi(ctx);
  if (!dbApi) {
    ctx.logger.logError('[db-passphrase] 未找到 DatabaseApi，请检查 NapCat 版本');
    return;
  }

  ctx.logger.log('[db-passphrase] 已加载，等待登录后获取数据库口令…');

  let done = false;
  const check = () => {
    try {
      if (done) return;
      if (dbApi.hasPassphrase && dbApi.hasPassphrase()) {
        const buf = dbApi.getPassphraseBuffer();
        const pass = buf ? buf.toString('ascii') : '';
        if (!pass) return;
        done = true;
        clearInterval(timer);
        ctx.logger.log('[db-passphrase] ============================================');
        ctx.logger.log(`[db-passphrase] 数据库口令 passphrase = ${pass}`);
        ctx.logger.log('[db-passphrase] ============================================');
        try {
          const dir = (typeof ctx.dataPath === 'string' && ctx.dataPath) || ctx.pluginPath;
          mkdirSync(dir, { recursive: true });
          const out = join(dir, 'passphrase.json');
          writeFileSync(
            out,
            JSON.stringify({ passphrase: pass, savedAt: new Date().toISOString() }, null, 2),
            'utf-8'
          );
          ctx.logger.log(`[db-passphrase] 已保存到 ${out}`);
        } catch (e) {
          ctx.logger.logError('[db-passphrase] 保存文件失败: ' + (e && e.message));
        }
      }
    } catch (e) {
      ctx.logger.logError('[db-passphrase] ' + (e && e.message));
    }
  };

  check(); // 已登录（重登缓存会话）时立刻捕获
  const timer = setInterval(check, 2000);
};

export const plugin_cleanup = () => {
  // 无需要清理的资源
};
