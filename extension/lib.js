/**
 * 纯函数集中放在这里：不碰 DOM、不碰 chrome API，因此可以在 node 里直接跑测试。
 *
 * 为什么单独拆一个文件：content script 里的函数包在 IIFE 里，外部拿不到，
 * 只能靠"在真实页面上点一下"来验证 —— 而 URL 解析、话术拼装这类逻辑
 * 恰好是最容易写错、又最容易用单测覆盖的部分。拆出来后这部分可以离线验证。
 *
 * 加载方式：manifest 的 content_scripts.js 按顺序注入，本文件在前，
 * 挂在 globalThis.JobAgentLib 上供 content.js 使用。
 */

(function (global) {
  'use strict';

  /**
   * 从详情页 URL 里取出岗位 ID，用于同源去重。
   * 三个平台的路径格式完全不同，只认 BOSS 那一种是错的：
   * 智联、猎聘取不到 ID 时 source_job_id 为空，去重索引失效，
   * 同一个岗位每抓一次就多一条记录。
   */
  function extractJobId(url) {
    if (!url) return null;
    const s = String(url);
    // BOSS：/job_detail/<id>.html
    let m = s.match(/job_detail\/([A-Za-z0-9~_-]+)\.html/i);
    if (m) return m[1];
    // 智联：/jobdetail/CC697082780J40896898510.htm
    m = s.match(/jobdetail\/([A-Za-z0-9~_-]+)\.htm/i);
    if (m) return m[1];
    // 猎聘：/job/123456.shtml 或 /a/123456.shtml
    m = s.match(/\/(?:job|a)\/(\d{5,})\.s?html/i);
    if (m) return m[1];
    // 兜底：查询串里的岗位标识
    m = s.match(/[?&](?:jobId|job_id|securityId|positionId)=([A-Za-z0-9~_-]+)/);
    return m ? m[1] : null;
  }

  /**
   * 拼打招呼话术。
   * 只用简历里真实存在的技能，不补任何原文没有的内容 ——
   * 这段文字发出去代表求职者本人，编造就等于替他撒谎。
   *
   * 另一个坑：之前写死"我有 N 年开发经验，主要技术栈是 X"。
   * 求职者可能去投门店经理/客服这类岗位，这句话会直接暴露"我在群发"。
   * 现在只在岗位确实对得上技术方向时才提技能，否则退回中性话术。
   */
  function buildGreeting(options) {
    const opts = options || {};
    const profile = opts.profile || {};
    const basic = profile.basic || {};
    const match = opts.match || {};

    const skills = (profile.skills || [])
      .map((s) => (typeof s === 'string' ? s : s.name))
      .filter(Boolean);
    const top = skills.slice(0, 4).join('、');
    const years = basic.work_years;
    const missed = match.missed_skills || [];
    const hitCount = match.skill_hit_count != null ? match.skill_hit_count : null;
    const mustCount = match.skill_must_count != null ? match.skill_must_count : null;

    // 只在后端给了明确计数时才判断"方向不对口"，不凭 missed 数量猜。
    // 猜错的代价是本来匹配的岗位被写成一句干巴巴的"想了解一下"
    const offDirection = hitCount !== null && mustCount !== null && hitCount === 0 && mustCount > 0;

    const lines = [];
    lines.push('您好，看到您这边在招' + (opts.title || '这个岗位') + '，想了解一下。');

    if (offDirection) {
      // 不说"我技能都对得上"，也不硬塞技术名词，只表达意愿 + 请对方说明重点
      if (years != null) lines.push('我这边有 ' + years + ' 年工作经验。');
      lines.push('想先了解下这个岗位最看重哪方面，方便我判断自己是否合适，谢谢。');
      return lines.join('');
    }

    if (years != null) {
      lines.push('我有 ' + years + ' 年工作经验，主要方向是' + (top || '（详见简历）') + '。');
    } else {
      lines.push('我的主要方向是' + (top || '（详见简历）') + '。');
    }
    if (!missed.length) {
      lines.push('岗位要求的技能我基本都有对应经验。');
    } else {
      lines.push('岗位要求的' + missed.join('、') + '我这边经验相对少一些，其余要求基本匹配。');
    }
    lines.push('如果方便的话，希望能进一步沟通，谢谢。');
    return lines.join('');
  }

  function cleanText(text) {
    return String(text == null ? '' : text)
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function escapeRegExp(text) {
    return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  global.JobAgentLib = {
    extractJobId: extractJobId,
    buildGreeting: buildGreeting,
    cleanText: cleanText,
    escapeRegExp: escapeRegExp,
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
