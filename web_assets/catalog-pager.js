/* Incremental catalogue paging. Filtering never silently stops at 100 rows. */
(function (root) {
    'use strict';
    class CatalogPager {
        constructor(fetchPage, matches = () => true, pageSize = 10) {
            this.fetchPage = fetchPage;
            this.matches = matches;
            this.pageSize = pageSize;
            this.skip = 0;
            this.total = Infinity;
            this.buffer = [];
            this.seen = new Set();
            this.busy = false;
        }
        get hasMore() { return this.buffer.length > 0 || this.skip < this.total; }
        async next() {
            if (this.busy) return [];
            this.busy = true;
            try {
                while (this.buffer.length < this.pageSize && this.skip < this.total) {
                    const data = await this.fetchPage(this.skip, this.pageSize);
                    const items = data.items || [];
                    this.total = data.total ?? this.skip + items.length;
                    this.skip += items.length;
                    if (!items.length) { this.total = this.skip; break; }
                    for (const item of items) {
                        if (!this.seen.has(item.record_id) && this.matches(item)) {
                            this.seen.add(item.record_id);
                            this.buffer.push(item);
                        }
                    }
                }
                return this.buffer.splice(0, this.pageSize);
            } finally { this.busy = false; }
        }
    }
    root.CatalogPager = CatalogPager;
    if (typeof module !== 'undefined') module.exports = {CatalogPager};
})(globalThis);
