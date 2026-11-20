<?php
// Smart Bulb Bridge setup page. Server-side validation; config is a plain JSON file.
$media = isset($settings['mediaDirectory']) ? $settings['mediaDirectory'] : '/home/fpp/media';
$cfgFile = "$media/config/fpp-smart-bulbs.json";
$statusFile = "$media/plugindata/SmartBulbBridge/run/status.json";
$msg = '';

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
    }
}
$cfg = file_exists($cfgFile) ? (json_decode(file_get_contents($cfgFile), true) ?: []) : [];
$bulbs = $cfg['bulbs'] ?? [];
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
</div>
<script>
const bulbs = <?= json_encode($bulbs) ?>, st = <?= json_encode($status['bulbs'] ?? []) ?>;
function addRow(b) {
  b = b || {protocol:'wiz', enabled:true};
  const tr = document.createElement('tr');
  const id = b.protocol==='wiz' ? b.ip : b.protocol==='matter' ? b.node_id : b.cync_id;
  const s = st.find(x => x.channel == b.channel);
  tr.innerHTML = `<td><input type=checkbox class=en ${b.enabled!==false?'checked':''}></td>
   <td><input class=nm size=14 value="${(b.name||'').replace(/"/g,'')}"></td>
   <td><select class=pr><option value=wiz>WiZ</option><option value=matter>Matter</option><option value=cync>Cync</option></select></td>
   <td><input class=ch type=number min=1 style="width:90px" value="${b.channel||''}"></td>
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
