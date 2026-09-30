# Offline dashboard dependencies

Chart.js 4.4.0 and Three.js 0.128.0 are the same versions previously loaded from
the CDN. Sources and SHA-256 hashes are recorded in `manifest.json`; downloaded
JavaScript files are unmodified. MIT licenses are stored alongside each library.

Caprasimo v6 and Figtree v9 use the original Google Fonts Latin/Latin Extended
WOFF2 resources. Their SIL Open Font License texts are in `fonts/`. The small
`fonts.css` changes resource URLs to local files and preserves font families,
weights and Unicode subsets. Cyrillic retains the existing system-font fallback.

The operator interface does not need these public hosts at runtime. Asset
updates are explicit: verify the source, retain its license and refresh hashes;
do not replace these files with a mutable latest URL. No package manager or
download is invoked when the application starts.
