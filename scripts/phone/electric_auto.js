/**
 * AutoX.js 脚本：定时打开「上海海洋大学一卡通」小程序，读电费余额，上报给树莓派。
 *
 * 用法：
 *   1. 手机装 AutoX.js（侧载 APK），授予「无障碍」权限；
 *   2. 把本文件放到 /sdcard/electric/electric_auto.js；
 *   3. 改下面 CFG 里的 url / key（与 config/secrets.toml 的 [ingest].key 一致）；
 *   4. AutoX 里运行本脚本，先看控制台输出，调通后再开「定时/自启动」。
 *
 * 设计要点：
 *   - 只读屏，不碰 token、不装证书、不 root；
 *   - 失败不重试太多次，交给树莓派的 check-stale 兜底告警；
 *   - 上报失败进本地队列，下次运行先补传，靠 server 端的 UNIQUE 去重。
 *
 * 注意：页面上的文字选择器要按你的手机实际界面校准（见 docs/phone-automation.md）。
 */

var CFG = {
    url: "http://192.168.1.10:8787/ingest",   // 树莓派的局域网 IP
    key: "PLEASE-CHANGE-ME-32-HEX",           // 与 secrets.toml 的 [ingest].key 一致
    shortcut: "上海海洋大学一卡通",            // 桌面快捷方式的文字
    intervalMinutes: 120,                     // 每 2 小时一次
    waitSeconds: 45,                          // 等小程序页面出现的上限
    queueDir: "/sdcard/electric",
    maxDeltaYuan: 20.0,                       // 与上次读数差太多就不上报，留着人工确认
    room: "12345678"
};

var AMOUNT = /^\s*(\d{1,4}\.\d{2})\s*元?\s*$/;
var LABELS = ["电费", "余额", "剩余", "寝室"];

// ---------------------------------------------------------------- 工具

function log(message) {
    console.log("[electric] " + message);
}

function ensureDir() {
    if (!files.exists(CFG.queueDir)) {
        files.ensureDir(CFG.queueDir + "/");
    }
}

function isoNow() {
    return new Date().toISOString();  // UTC；服务端会统一换算成 Asia/Shanghai
}

/** 找与「电费/余额」标签最近的金额节点；有歧义就返回 null，宁可失败也不猜。 */
function readAmount() {
    var byText = textMatches(AMOUNT).find();
    var byDesc = descMatches(AMOUNT).find();
    var candidates = byText.concat(byDesc);
    if (candidates.length === 0) {
        return null;
    }

    var labels = [];
    for (var i = 0; i < LABELS.length; i++) {
        labels = labels.concat(textContains(LABELS[i]).find());
    }

    function distance(node) {
        var b = node.bounds();
        var best = 1e9;
        for (var j = 0; j < labels.length; j++) {
            var lb = labels[j].bounds();
            var d = Math.abs(b.top - lb.bottom) + Math.abs(b.left - lb.left);
            if (d < best) best = d;
        }
        return best;
    }

    if (labels.length > 0) {
        candidates.sort(function (a, b) { return distance(a) - distance(b); });
        if (candidates.length > 1 && distance(candidates[0]) === distance(candidates[1])) {
            log("多个金额节点离标签一样近，放弃本次");
            return null;
        }
    } else if (candidates.length > 1) {
        log("没有标签且候选金额不止一个，放弃本次");
        return null;
    }

    var text = candidates[0].text() || candidates[0].desc() || "";
    var match = AMOUNT.exec(text);
    return match ? match[1] : null;
}

function openMiniProgram() {
    device.wakeUp();
    sleep(800);
    home();
    sleep(1500);

    var entry = textContains(CFG.shortcut).findOne(8000);
    if (!entry) {
        // 兜底：直接从微信里进（慢且易变，仅当桌面没有快捷方式时）
        log("桌面没有快捷方式，尝试从微信启动");
        app.launch("com.tencent.mm");
        sleep(3000);
        var tab = textContains("小程序").findOne(5000);
        if (!tab) return false;
        click(tab.bounds().centerX(), tab.bounds().centerY());
        sleep(2000);
        entry = textContains(CFG.shortcut).findOne(5000);
        if (!entry) return false;
    }
    click(entry.bounds().centerX(), entry.bounds().centerY());
    return true;
}

function waitAmount() {
    var deadline = new Date().getTime() + CFG.waitSeconds * 1000;
    while (new Date().getTime() < deadline) {
        var value = readAmount();
        if (value) return value;
        sleep(2000);
    }
    return null;
}

// ---------------------------------------------------------------- 上报

function buildPayload(value) {
    return {
        room_key: CFG.room,
        balance: value,
        source: "phone",
        captured_at: isoNow(),
        device: device.model || "android"
    };
}

function post(payload) {
    var res = http.postJson(CFG.url, payload, {
        headers: { "X-Ingest-Key": CFG.key }
    });
    var code = res.statusCode;
    var body = "";
    try { body = res.body.string(); } catch (e) { body = ""; }
    return { code: code, body: body };
}

function enqueue(payload) {
    ensureDir();
    files.append(CFG.queueDir + "/queue.jsonl", JSON.stringify(payload) + "\n");
}

/** 先补传历史队列，再发本次。4xx 视为永久失败（丢弃并记录），其余留在队列里。 */
function flushQueue() {
    var path = CFG.queueDir + "/queue.jsonl";
    if (!files.exists(path)) return;
    var lines = files.read(path).split("\n");
    var keep = [];
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) continue;
        var payload;
        try { payload = JSON.parse(line); } catch (e) { continue; }
        var result = post(payload);
        if (result.code === 200 || (result.code >= 400 && result.code < 500 && result.code !== 429)) {
            if (result.code !== 200) {
                files.append(CFG.queueDir + "/rejected.jsonl", line + "\n");
                log("补传被拒绝（" + result.code + "）：" + result.body);
            }
        } else {
            keep.push(line);
        }
        sleep(500);
    }
    files.write(path, keep.length ? keep.join("\n") + "\n" : "");
}

// ---------------------------------------------------------------- 主流程

function runOnce() {
    log("开始采集");
    if (!openMiniProgram()) {
        log("打不开小程序，本次放弃");
        return false;
    }

    var value = waitAmount();
    if (!value) {
        log("没读到余额（页面结构可能变了）");
        try {
            images.save(captureScreen(), CFG.queueDir + "/fail-" + new Date().getTime() + ".png", "png", 80);
        } catch (e) {
            log("截图也失败了：" + e);
        }
        return false;
    }

    var number = parseFloat(value);
    if (!(number >= 0 && number <= 10000)) {
        log("读到的数值不合理：" + value);
        return false;
    }
    log("读到余额：" + value + " 元");

    // 只构造一次 payload：重试/补传必须带同一个 captured_at，服务端才能去重
    var payload = buildPayload(value);
    flushQueue();
    var result = post(payload);
    if (result.code === 200) {
        log("上报成功：" + result.body);
        return true;
    }
    log("上报失败（" + result.code + "），进入队列：" + result.body);
    enqueue(payload);
    return false;
}

ensureDir();

while (true) {
    try {
        runOnce();
    } catch (e) {
        log("异常：" + e);
    }
    log("休息 " + CFG.intervalMinutes + " 分钟");
    sleep(CFG.intervalMinutes * 60 * 1000);
}
