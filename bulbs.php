<?php
// Smart Bulb Bridge setup page. Server-side validation; config is a plain JSON file.
$media = isset($settings['mediaDirectory']) ? $settings['mediaDirectory'] : '/home/fpp/media';
$cfgFile = "$media/config/fpp-smart-bulbs.json";
$statusFile = "$media/plugindata/SmartBulbBridge/run/status.json";
$msg = '';

// ---- Cync account setup (install support, sign in, enter emailed code) ----
$cyncDir = "$media/plugindata/SmartBulbBridge/cync";
$cyncPy = "$cyncDir/venv/bin/python3";
$cyncMsg = '';
function cync_json($f) { return file_exists($f) ? (json_decode(@file_get_contents($f), true) ?: null) : null; }
function cync_private_write($f, $data) { $old = umask(0077); file_put_contents($f, $data); umask($old); @chmod($f, 0600); }
function cync_launch($cmd) { exec('nohup ' . $cmd . ' >/dev/null 2>&1 &'); }
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['cync_action'])) {
    $act = $_POST['cync_action'];
    if (!is_dir($cyncDir)) { @mkdir($cyncDir, 0700, true); }
    @chmod($cyncDir, 0700);
    if ($act === 'install') {
        cync_private_write("$cyncDir/install.state", "running\n");
        cync_launch('bash ' . escapeshellarg(dirname(__FILE__) . '/scripts/cync_install.sh') . ' ' . escapeshellarg($cyncDir));
        $cyncMsg = 'Installing Cync support. This can take a few minutes.';
    } elseif ($act === 'login') {
        $email = trim($_POST['cync_email'] ?? '');
        $pw = $_POST['cync_password'] ?? '';
        if (!filter_var($email, FILTER_VALIDATE_EMAIL) || $pw === '' || strlen($pw) > 1024) {
            $cyncMsg = 'Enter the email address and password of your Cync account.';
        } elseif (!file_exists($cyncPy)) {
            $cyncMsg = 'Install Cync support first.';
        } else {
            @unlink("$cyncDir/code-request.txt");
            cync_private_write("$cyncDir/onboard-status.json", json_encode(['state' => 'working', 'message' => 'Starting...']));
            cync_private_write("$cyncDir/login-request.json", json_encode(['email' => $email, 'password' => $pw]));
            cync_launch(escapeshellarg($cyncPy) . ' -B ' . escapeshellarg(dirname(__FILE__) . '/bridge/cync_web.py')
                        . ' --dir ' . escapeshellarg($cyncDir));
            $cyncMsg = 'Signing in to Cync...';
        }
    } elseif ($act === 'code') {
        $code = trim($_POST['cync_code'] ?? '');
        if (!preg_match('/^\d{4,8}$/', $code)) { $cyncMsg = 'The code is 4 to 8 digits.'; }
        else { cync_private_write("$cyncDir/code-request.txt", $code . "\n"); $cyncMsg = 'Checking the code...'; }
    } elseif ($act === 'forget') {
        foreach (['auth.json', 'cync-session.json', 'onboard-status.json', 'login-request.json', 'code-request.txt'] as $f) { @unlink("$cyncDir/$f"); }
        exec('bash ' . escapeshellarg(dirname(__FILE__) . '/scripts/restart.sh') . ' >/dev/null 2>&1');
        $cyncMsg = 'Cync account removed from this FPP and the bridge was restarted.';
    }
}
$cyncInstalled = file_exists($cyncPy);
$installState = trim((string)@file_get_contents("$cyncDir/install.state"));
if ($installState === 'running' && (time() - (int)@filemtime("$cyncDir/install.state")) > 1200) { $installState = 'failed'; }
$cyncOnb = cync_json("$cyncDir/onboard-status.json");
if ($cyncOnb && in_array($cyncOnb['state'] ?? '', ['working', 'need_code'], true)
    && (time() - (int)@filemtime("$cyncDir/onboard-status.json")) > 900) {
    $cyncOnb = ['state' => 'error', 'message' => 'That sign-in timed out. Please start again.'];
}
$cyncAuth = file_exists("$cyncDir/auth.json");
$cyncBusy = ($installState === 'running') || ($cyncOnb && ($cyncOnb['state'] ?? '') === 'working');

function validate_bulbs($rows, &$err) {
    $out = []; $used = [];
    foreach ($rows as $r) {
        $p = $r['protocol'] ?? '';
        $ch = filter_var($r['channel'] ?? '', FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 8388608]]);
        if ($ch === false) { $err = 'Channel must be a whole number ≥ 1'; return null; }
        for ($c = $ch; $c < $ch + 3; $c++) {
            if (isset($used[$c])) { $err = "Channel $c is used twice"; return null; }
            $used[$c] = 1;
        }
        $b = ['name' => substr(preg_replace('/[^\w .-]/u', '', $r['name'] ?? ''), 0, 40) ?: "$p-$ch",
              'protocol' => $p, 'channel' => $ch, 'enabled' => !empty($r['enabled'])];
        if ($p === 'wiz') {
            $ip = filter_var($r['ip'] ?? '', FILTER_VALIDATE_IP, FILTER_FLAG_IPV4);
            if (!$ip) { $err = 'WiZ bulbs need an IPv4 address'; return null; }
            $b['ip'] = $ip;
        } elseif ($p === 'matter') {
            $n = filter_var($r['node_id'] ?? '', FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
            if ($n === false) { $err = 'Matter bulbs need a numeric node ID'; return null; }
            $b['node_id'] = $n; $b['endpoint_id'] = (int)($r['endpoint_id'] ?? 1) ?: 1;
        } elseif ($p === 'cync') {
            $n = filter_var($r['cync_id'] ?? '', FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
            if ($n === false) { $err = 'Cync bulbs need a numeric device ID'; return null; }
            $b['cync_id'] = $n;
        } else { $err = 'Unknown protocol'; return null; }
        $out[] = $b;
    }
    return $out;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['bulbs_json'])) {
    $rows = json_decode($_POST['bulbs_json'], true);
    $err = '';
    $bulbs = is_array($rows) ? validate_bulbs($rows, $err) : null;
    if ($bulbs === null) { $msg = 'Not saved: ' . ($err ?: 'bad data'); }
    else {
        $cfg = file_exists($cfgFile) ? (json_decode(file_get_contents($cfgFile), true) ?: []) : [];
        $cfg['bulbs'] = $bulbs;
        $cfg['matter_url'] = 'ws://127.0.0.1:5580/ws';
        $cfg['cync_socket'] = "$media/plugindata/SmartBulbBridge/cync/bridge.sock";
        @mkdir(dirname($cfgFile), 0775, true);
        file_put_contents($cfgFile . '.tmp', json_encode($cfg, JSON_PRETTY_PRINT));
        rename($cfgFile . '.tmp', $cfgFile);
        $syncOut = [];
        exec('python3 ' . escapeshellarg(dirname(__FILE__) . '/tools/sync_output.py') . ' ' . escapeshellarg($cfgFile)
             . ' ' . escapeshellarg("$media/config/channeloutputs.json") . ' 2>&1', $syncOut, $syncRc);
        $syncMsg = trim(implode(' ', $syncOut));
        exec('bash ' . escapeshellarg(dirname(__FILE__) . '/scripts/restart.sh') . ' >/dev/null 2>&1', $out, $rc);
        // wait (up to ~6 s) for the new daemon to write its first status report
        $up = false;
        for ($i = 0; $i < 12 && !$up; $i++) {
            clearstatcache();
            $st = file_exists($statusFile) ? json_decode(@file_get_contents($statusFile), true) : null;
            $up = $st && isset($st['time']) && (time() - $st['time'] < 10);
            if (!$up) usleep(500000);
        }
        $msg = $up ? 'Saved. The bridge was restarted and is running.'
                   : 'Saved, but the bridge did not report back. See ' . $media . '/logs/fpp-smart-bulbs.log';
        $msg .= ' ' . $syncMsg;
    }
}
$cfg = file_exists($cfgFile) ? (json_decode(file_get_contents($cfgFile), true) ?: []) : [];
$bulbs = $cfg['bulbs'] ?? [];
$found = null;
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['discover'])) {
    $discOut = [];
    exec('python3 ' . escapeshellarg(dirname(__FILE__) . '/tools/wiz_discover.py') . ' 2>/dev/null', $discOut);
    $found = json_decode(implode('', $discOut), true);
    if (!is_array($found)) { $found = []; }
}
$status = file_exists($statusFile) ? json_decode(file_get_contents($statusFile), true) : null;
$running = $status && (time() - $status['time'] < 10);
?>
<div class="container-fluid">
<h2>Smart Bulb Bridge</h2>
<p>Daemon: <b><?= $running ? 'running' : 'not running' ?></b>
<?php if ($status) echo " — {$status['packets']} packets received"; ?></p>
<?php if ($msg) echo '<div class="alert alert-info">' . htmlspecialchars($msg) . '</div>'; ?>
<p>Each bulb uses <b>3 consecutive FPP channels</b> (R,G,B). Add a <i>SmartBulbBridge</i> channel output
covering exactly those channels (Input/Output Setup → Channel Outputs).</p>
<table class="table" id="t"><thead><tr><th>On</th><th>Name</th><th>Type</th><th>First channel</th>
<th>IP / Node ID / Cync ID</th><th>Last sent</th><th></th></tr></thead><tbody></tbody></table>
<button class="buttons" onclick="addRow()">Add bulb</button>
<button class="buttons btn-success" onclick="save()">Save &amp; restart bridge</button>
<form id="f" method="post"><input type="hidden" name="bulbs_json" id="bj"></form>
<form method="post" style="display:inline"><button class="buttons" name="discover" value="1">Find WiZ bulbs</button></form>
<?php if ($found !== null) { ?>
  <h4 style="margin-top:1em">WiZ bulbs found on your network</h4>
  <?php if (count($found) === 0) { ?>
    <p>None answered. Check that <b>Local Communication</b> is enabled in the WiZ app, the bulbs are on, and they are on the same network as FPP.</p>
  <?php } else { ?>
    <p>Save any changes above first, because looking for bulbs reloads this page.</p>
    <table class="table"><thead><tr><th>IP address</th><th>MAC</th><th></th></tr></thead><tbody>
    <?php foreach ($found as $f) { $ip = htmlspecialchars($f['ip'] ?? ''); $mac = htmlspecialchars($f['mac'] ?? ''); ?>
      <tr><td><?= $ip ?></td><td><?= $mac ?></td>
      <td><button class="buttons" type="button" onclick="addRow({protocol:'wiz', enabled:true, ip:'<?= $ip ?>'})">Add as a bulb</button></td></tr>
    <?php } ?>
    </tbody></table>
  <?php } ?>
<?php } ?>
<hr>
<h3>Cync bulbs</h3>
<?php if ($cyncMsg) echo '<div class="alert alert-info">' . htmlspecialchars($cyncMsg) . '</div>'; ?>
<?php if (!$cyncInstalled) { ?>
  <?php if ($installState === 'running') { ?>
    <p>Installing Cync support... this can take a few minutes. This page refreshes by itself.</p>
  <?php } else { ?>
    <?php if ($installState === 'failed') { ?>
      <div class="alert alert-warning">The install did not finish. Last lines of its log:
      <pre style="white-space:pre-wrap"><?= htmlspecialchars(implode("\n", array_slice(file_exists("$cyncDir/install.log") ? file("$cyncDir/install.log", FILE_IGNORE_NEW_LINES) : [], -6))) ?></pre>
      It needs FPP 10.4 or newer (Python 3.13) and an internet connection.</div>
    <?php } ?>
    <p>Cync bulbs are controlled through the Cync cloud. First, this plugin installs a small private Cync library
    (downloaded from the internet, not part of FPP's own Python).</p>
    <form method="post"><button class="buttons" name="cync_action" value="install">Install Cync support</button></form>
  <?php } ?>
<?php } else { ?>
  <?php $onbState = $cyncOnb['state'] ?? ''; ?>
  <?php if ($onbState === 'need_code') { ?>
    <p><b>Cync emailed you a verification code.</b> Enter it here:</p>
    <form method="post"><input name="cync_code" size="10" inputmode="numeric" autocomplete="one-time-code" autofocus>
      <button class="buttons" name="cync_action" value="code">Submit code</button></form>
  <?php } elseif ($onbState === 'working') { ?>
    <p><?= htmlspecialchars($cyncOnb['message'] ?? 'Working...') ?> This page refreshes by itself.</p>
  <?php } else { ?>
    <?php if ($onbState === 'error') echo '<div class="alert alert-warning">' . htmlspecialchars($cyncOnb['message'] ?? 'Sign-in failed.') . '</div>'; ?>
    <?php if ($cyncAuth) { ?>
      <p>A Cync account is saved on this FPP. To refresh the list of bulbs, sign in again below (Cync may ask for another emailed code).</p>
    <?php } else { ?>
      <p>Sign in with the email address and password of your Cync account. Cync allows <b>only one connection per account</b>,
      so this can sign you out of the Cync phone app, and the plugin cannot run alongside another program that uses the same account.
      The password is kept in a private file on this FPP and is used to sign in. FPP is usually served over plain HTTP,
      so it crosses your local network unencrypted.</p>
    <?php } ?>
    <form method="post" autocomplete="off">
      <input type="email" name="cync_email" placeholder="Cync email" size="28">
      <input type="password" name="cync_password" placeholder="Cync password" size="20" autocomplete="new-password">
      <button class="buttons" name="cync_action" value="login">Sign in to Cync</button></form>
  <?php } ?>
  <?php if ($onbState === 'done' && !empty($cyncOnb['devices'])) { ?>
    <p><b><?= htmlspecialchars($cyncOnb['message'] ?? '') ?></b> Adding a bulb from this list fills in a new row in the table above. Click <i>Save &amp; restart bridge</i> afterwards.</p>
    <table class="table"><thead><tr><th>Name in the Cync app</th><th>Device ID</th><th></th></tr></thead><tbody>
    <?php foreach ($cyncOnb['devices'] as $dv) { $row = ['protocol' => 'cync', 'enabled' => true, 'cync_id' => $dv['id'], 'name' => $dv['name']]; ?>
      <tr><td><?= htmlspecialchars($dv['name']) ?></td><td><?= htmlspecialchars((string)$dv['id']) ?></td>
      <td><button class="buttons" type="button" onclick='addRow(<?= htmlspecialchars(json_encode($row), ENT_QUOTES) ?>)'>Add as a bulb</button></td></tr>
    <?php } ?>
    </tbody></table>
  <?php } ?>
  <?php if ($cyncAuth && !$cyncBusy && $onbState !== 'need_code') { ?>
    <form method="post" style="margin-top:.5em"><button class="buttons" name="cync_action" value="forget"
      onclick="return confirm('Remove the saved Cync account from this FPP?')">Forget the saved Cync account</button></form>
  <?php } ?>
<?php } ?>
</div>
<script>
<?php if ($cyncBusy) { ?>
setTimeout(function () { location.replace(location.href); }, 4000);
<?php } ?>
const bulbs = <?= json_encode($bulbs) ?>, st = <?= json_encode($status['bulbs'] ?? []) ?>;
function nextChannel() {
  let n = 0;
  document.querySelectorAll('#t .ch').forEach(i => { const v = parseInt(i.value); if (v > 0 && v + 3 > n) n = v + 3; });
  return n || '';
}
function addRow(b) {
  b = b || {protocol:'wiz', enabled:true};
  const tr = document.createElement('tr');
  const id = b.protocol==='wiz' ? b.ip : b.protocol==='matter' ? b.node_id : b.cync_id;
  const s = st.find(x => x.channel == b.channel);
  tr.innerHTML = `<td><input type=checkbox class=en ${b.enabled!==false?'checked':''}></td>
   <td><input class=nm size=14 value="${(b.name||'').replace(/"/g,'')}"></td>
   <td><select class=pr><option value=wiz>WiZ</option><option value=matter>Matter</option><option value=cync>Cync</option></select></td>
   <td><input class=ch type=number min=1 style="width:90px" value="${b.channel||nextChannel()}"></td>
   <td><input class=id size=16 value="${id||''}"></td>
   <td>${s && s.rgb ? 'rgb('+s.rgb+')' : ''}${s && s.fail ? ' ⚠ '+s.last_error : ''}</td>
   <td><button class=buttons onclick="this.closest('tr').remove()">Remove</button></td>`;
  tr.querySelector('.pr').value = b.protocol;
  document.querySelector('#t tbody').appendChild(tr);
}
function save() {
  const rows = [...document.querySelectorAll('#t tbody tr')].map(tr => {
    const p = tr.querySelector('.pr').value, id = tr.querySelector('.id').value.trim();
    const r = {enabled: tr.querySelector('.en').checked, name: tr.querySelector('.nm').value,
               protocol: p, channel: tr.querySelector('.ch').value};
    if (p==='wiz') r.ip = id; else if (p==='matter') r.node_id = id; else r.cync_id = id;
    return r; });
  document.getElementById('bj').value = JSON.stringify(rows);
  document.getElementById('f').submit();
}
bulbs.forEach(addRow);
</script>
