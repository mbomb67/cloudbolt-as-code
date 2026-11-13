# CloudBolt Engine Budgets (XUI)

Adds a **Budgets** tab to the Group page. It sets a monthly budget for the group in CloudBolt Engine and shows spend so far, the projected month-end total, headroom, budget against actual for the last 12 months, and the current month's spend with the projection.

## Setup
1. In CloudBolt Engine, open **Settings > API Keys** and create an API key with the **Manage budgets and reports** permission. Copy the client ID and client secret.
2. In CloudBolt, open **Admin > Connection Info** and create a connection with label `cloudbolt_engine`, protocol `https`, IP/Hostname the CloudBolt Engine API host (`<your-engine-api-host>`, the host that serves the API and `/oauth/token`, not the web app you sign in to), username the client ID, and password the client secret.
3. Sync the **Refresh CloudBolt Engine Budgets** recurring job ([RJB-wxwp58f2](../../recurring_jobs/RJB-wxwp58f2/)), which brings the `cloudbolt_engine` shared module ([SHM-6v7fxtdd](../../shared_modules/SHM-6v7fxtdd/)), and sync this extension.
4. Restart the web server (`systemctl restart httpd`): the extension adds URL paths, and the web process keeps shared modules it already loaded.
5. Open a group, select **Budgets**, fill in the **Set a budget** form, and choose **Set budget**.

## How the budget is scoped
By default a budget is scoped to the tag `cloudbolt_group` with the value the form shows. You can instead enter one or more tags your resources already carry (up to five). A budget counts only cost data that carries every tag listed, and keys and values must match exactly, including upper and lower case. CloudBolt Engine creates the budget once every tag key appears in billing data; until then the tab shows it as pending and names the keys still missing. The recurring job refreshes every budget every four hours and creates pending budgets as soon as their tags arrive. Tags can be changed while the budget is pending; to change them afterwards, remove the budget and set it again.

## Who can do what
- **View:** CB Admins and people with the `group.view` permission on the group.
- **Set, edit and remove:** CB Admins and people with the `group.change_attributes` permission on the group. Removing deletes the budget in CloudBolt Engine.
- **Refresh:** managers at any time; viewers when the stored data is more than five minutes old. The tab loads its data when you first select it.

## Limits
- Cost data lags about two days, so spend so far runs behind today.
- The month-end projection is spend so far plus the mean daily spend of the last seven days for the remaining days.
- A group has one budget, in one currency. The currency is set when the budget is created.
