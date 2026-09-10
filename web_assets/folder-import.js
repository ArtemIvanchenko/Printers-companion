/* A folder manifest is input data, never authority to relink a database card. */
(function (root) {
    'use strict';
    const pathOf = file => file._importPath || file.webkitRelativePath || file.name;
    const safePath = path => typeof path === 'string' && path.length > 0
        && !path.startsWith('/') && !path.includes('\\') && !path.includes(':')
        && path.split('/').every(part => part && part !== '.' && part !== '..');
    async function plan(files) {
        files = [...files].filter(file => !file.name.startsWith('._') && file.name !== '.DS_Store');
        if (!files.length) throw new Error('Папка пуста');
        const byPath = new Map(files.map(file => [pathOf(file), file]));
        if (byPath.size !== files.length || files.some(file => !safePath(pathOf(file)))) throw new Error('Недопустимые или повторяющиеся пути файлов');
        const manifests = files.filter(file => file.name === 'print-bundle.json')
            .sort((a, b) => pathOf(a).split('/').length - pathOf(b).split('/').length);
        if (manifests.length) {
            const first = manifests[0], path = pathOf(first), prefix = path.slice(0, -first.name.length);
            if (manifests.filter(file => pathOf(file).split('/').length === path.split('/').length).length !== 1)
                throw new Error('Выбрано несколько печатей. Выберите одну папку печати или модели.');
            if (first.size > 2 * 1024 * 1024) throw new Error('Слишком большой манифест: выберите одну папку печати');
            const manifest = JSON.parse(await first.text());
            if (manifest.schema_version !== 1) throw new Error('Неизвестная версия описания папки');
            if (manifest.kind === 'catalog') throw new Error('Это весь каталог. Выберите одну папку внутри раздела 01 или 02.');
            if (manifest.kind === 'model_reference') throw new Error('Логи относятся ко всей плите. Выберите родительскую папку печати, не одну её деталь.');
            if (!['print', 'model', 'logs'].includes(manifest.kind) || !Array.isArray(manifest.files) || !manifest.files.length)
                throw new Error('Неполное описание папки');
            const selected = [], seen = new Set(), expectations = new Map();
            for (const row of manifest.files) {
                if (!safePath(row.path) || !/^[a-f0-9]{64}$/.test(row.sha256) || !Number.isSafeInteger(row.size) || row.size < 0)
                    throw new Error('Некорректный путь, размер или SHA-256 в описании');
                const file = byPath.get(prefix + row.path);
                if (!file || file.size !== row.size) throw new Error(`Файл отсутствует или изменился: ${row.path}`);
                const isLog = /\.(log|zip)$/i.test(file.name);
                if (!['logs', 'model'].includes(row.role) || (row.role === 'logs') !== isLog
                        || (row.role === 'model' && !/\.(stl|magics|mgx)$/i.test(file.name)))
                    throw new Error(`Тип файла не соответствует описанию: ${row.path}`);
                if (seen.has(row.sha256)) continue;
                seen.add(row.sha256); selected.push(file); expectations.set(file, row.sha256);
            }
            return {files: selected, manifest, expectations, description: `${manifest.name || 'Папка'}: ${selected.length} файлов. ${manifest.warning_ru || ''}`};
        }
        const selected = files.filter(file => /\.(stl|magics|mgx|log|zip|png|jpe?g|webp|heic|pdf|txt)$/i.test(file.name));
        if (!selected.length) throw new Error('Нет поддерживаемых моделей, логов или вложений');
        if (selected.filter(file => /\.(magics|mgx)$/i.test(file.name)).length > 1)
            throw new Error('Найдены несколько Magics-компоновок. Разделите их по печатям.');
        return {files: selected, manifest: null, expectations: new Map(), description:
            `${selected.length} файлов без описания связи. Подтвердите, что они относятся к одной печати; дата этого не доказывает.`};
    }
    async function droppedFiles(items) {
        const files = [];
        async function visit(entry, prefix = '') {
            if (entry.isFile) {
                const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
                Object.defineProperty(file, '_importPath', {value: prefix + file.name}); files.push(file);
            } else if (entry.isDirectory) {
                const reader = entry.createReader();
                for (;;) {
                    const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
                    if (!entries.length) break;
                    for (const child of entries) await visit(child, prefix + entry.name + '/');
                }
            }
        }
        for (const item of items) {
            const entry = item.webkitGetAsEntry?.();
            if (entry) await visit(entry);
            else { const file = item.getAsFile?.(); if (file) files.push(file); }
        }
        return files;
    }
    root.PrinterFolderImport = {plan, pathOf, droppedFiles};
    if (typeof module !== 'undefined') module.exports = root.PrinterFolderImport;
})(globalThis);
