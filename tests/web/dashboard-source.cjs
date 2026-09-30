const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
function dashboardSource() {
    const html = fs.readFileSync(path.join(root, 'web_templates/dashboard.html'), 'utf8');
    const names = [...html.matchAll(/src="\/assets\/(dashboard\/[^\"]+\.js)"/g)].map(m=>m[1]);
    return html + '\n' + names.map(name=>fs.readFileSync(path.join(root, 'web_assets', name), 'utf8')).join('\n');
}
module.exports = {dashboardSource};
