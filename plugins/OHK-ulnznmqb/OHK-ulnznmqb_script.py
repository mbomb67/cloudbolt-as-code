"""Blueprint build item: deploys the Request Log sample app on the app tier and points it at the database.

The app is one PHP page: the name of the CloudBolt resource it belongs to, a form that adds an
entry, a table of all entries from MariaDB, and a footer with the hostname of the server that
answered and its PHP version. Its database settings and the resource's name are
written to /etc/request-log/db.json and name the database tier's Service, never an IP address,
because a VM's pod IP changes when it restarts. The database password is read from the resource's
web_app_db_password parameter, where the Install MariaDB step stores it. Runs through the OpenShift
API tunnel (shared_modules/SHM-wkubiao7).

Inputs: tier (default App), db_tier (default DB), database_name, database_user, app_title.
"""

import json

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "App")
DB_TIER = tiers.clean("{{ db_tier }}", "DB")
DATABASE = tiers.clean("{{ database_name }}", "requestlog")
USER = tiers.clean("{{ database_user }}", "requestlog")
TITLE = tiers.clean("{{ app_title }}", "Request Log")
PASSWORD_PARAMETER = "web_app_db_password"
CONFIG_PATH = "/etc/request-log/db.json"

INDEX_PHP = r"""<?php
$config = json_decode(file_get_contents('/etc/request-log/db.json'), true);
$title = $config['title'];
$deployment = $config['deployment'] ?? '';
$entries = [];
$error = null;
mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);
try {
    $db = new mysqli($config['host'], $config['user'], $config['password'], $config['database'], (int) $config['port']);
    $db->set_charset('utf8mb4');
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        $name = substr(trim($_POST['name'] ?? ''), 0, 100);
        $message = substr(trim($_POST['message'] ?? ''), 0, 500);
        if ($name !== '' && $message !== '') {
            $insert = $db->prepare('INSERT INTO entries (name, message) VALUES (?, ?)');
            $insert->bind_param('ss', $name, $message);
            $insert->execute();
        }
        header('Location: /', true, 303);
        exit;
    }
    $entries = $db->query('SELECT name, message, created_at FROM entries ORDER BY created_at DESC, id DESC LIMIT 200')
        ->fetch_all(MYSQLI_ASSOC);
} catch (Throwable $exception) {
    $error = $exception->getMessage();
}
function h($text) { return htmlspecialchars((string) $text, ENT_QUOTES, 'UTF-8'); }
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= h($deployment === '' ? $title : $deployment . ' | ' . $title) ?></title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; margin: 0; background: #f5f6f8; color: #1d2733; }
  main { max-width: 760px; margin: 0 auto; padding: 32px 16px; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
  h1 { font-size: 24px; margin: 0; }
  .deployment { margin: 0; padding: 4px 12px; border: 1px solid #056159; border-radius: 999px; background: #e3f6f2; color: #056159; font-size: 14px; }
  form { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
  input { font: inherit; padding: 8px 10px; border: 1px solid #c3cad3; border-radius: 6px; }
  input[name=name] { flex: 1 1 140px; }
  input[name=message] { flex: 3 1 260px; }
  button { font: inherit; padding: 8px 16px; border: 0; border-radius: 6px; background: #056159; color: #fff; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #e6e9ee; vertical-align: top; }
  th { background: #eef1f4; font-weight: 600; }
  td.when { white-space: nowrap; color: #5a6676; }
  .error { background: #fde8ec; color: #8a1730; padding: 12px; border-radius: 8px; }
  footer { margin-top: 24px; color: #5a6676; font-size: 14px; }
</style>
</head>
<body>
<main>
  <header>
    <h1><?= h($title) ?></h1>
    <?php if ($deployment !== ''): ?>
      <p class="deployment">Deployment <strong><?= h($deployment) ?></strong></p>
    <?php endif; ?>
  </header>
  <?php if ($error !== null): ?>
    <p class="error">Database error: <?= h($error) ?></p>
  <?php else: ?>
    <form method="post" action="/">
      <input name="name" placeholder="Your name" maxlength="100" required>
      <input name="message" placeholder="Short message" maxlength="500" required>
      <button type="submit">Add entry</button>
    </form>
    <table id="entries">
      <thead><tr><th>When</th><th>Name</th><th>Message</th></tr></thead>
      <tbody>
      <?php foreach ($entries as $entry): ?>
        <tr><td class="when"><?= h($entry['created_at']) ?></td><td><?= h($entry['name']) ?></td><td><?= h($entry['message']) ?></td></tr>
      <?php endforeach; ?>
      <?php if (!$entries): ?>
        <tr><td colspan="3">No entries yet.</td></tr>
      <?php endif; ?>
      </tbody>
    </table>
  <?php endif; ?>
  <footer>Served by <strong><?= h(gethostname()) ?></strong> on PHP <strong><?= h(PHP_VERSION) ?></strong></footer>
</main>
</body>
</html>
"""


def run(job, resource=None, **kwargs):
    try:
        server = tiers.tier_server(resource, TIER)
        db_server = tiers.tier_server(resource, DB_TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    password = resource.get_value_for_custom_field(PASSWORD_PARAMETER)
    if not password:
        return "FAILURE", "", f"The resource has no {PASSWORD_PARAMETER} parameter; the Install MariaDB step stores it."

    db_host = vmnet.service_fqdn(db_server.hostname, db_server.ovirtserverinfo.namespace)
    config = json.dumps({
        "title": TITLE,
        "deployment": resource.name,
        "host": db_host,
        "port": 3306,
        "database": DATABASE,
        "user": USER,
        "password": password,
    })
    script = (
        # Readable by the web server's group only: it holds the database password.
        tiers.write_file(CONFIG_PATH, config, mode="0640", group="apache")
        + tiers.write_file("/var/www/html/index.php", INDEX_PHP)
        + "port=$(sudo awk '/^Listen /{print $2; exit}' /etc/httpd/conf/httpd.conf)\n"
        + 'page=$(curl -sS --max-time 20 "http://localhost:${port}/")\n'
        + "if echo \"$page\" | grep -q 'class=\"error\"'; then\n"
        + "  echo \"$page\" | grep 'class=\"error\"' | sed 's/<[^>]*>//g'\n"
        + "  exit 1\n"
        + "fi\n"
        + "echo \"$page\" | grep -q 'id=\"entries\"'\n"
        + "echo \"The app answers on port ${port} and reads its entries from the database.\"\n"
        + "echo \"$page\" | grep -o 'Deployment <strong>.*' | sed 's/<[^>]*>//g'\n"
        + "echo \"$page\" | grep -o 'Served by.*' | sed 's/<[^>]*>//g'\n"
    )

    set_progress(f"Deploying the app on {server.hostname} with its database at {db_host}")
    try:
        tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    return "SUCCESS", f"The app is deployed on {server.hostname} and connected to {db_host}.", ""
