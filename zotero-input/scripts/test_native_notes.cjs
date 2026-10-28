// Test-only Zotero data API simulation; no desktop connection or disk writes.
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const {script, job, mode, secondScript} = input;
const plan = job.notes[0];
const db = new Map();
let creates = 0;
const internalID = plan.item.library.type === "group" ? 2 : 1;
const parent = {
    key: plan.item.item_key, id: 1, libraryID: internalID, deleted: false,
    isRegularItem: () => true,
    getCollections: () => mode === "wrong_collection" || (mode === "changed_collection" && creates) ? [] : [11],
    getNotes: () => [...db.keys()],
    getCreators: () => mode === "wrong_author" ? [{lastName: "Another"}]
        : mode === "split_cjk_author" ? [{lastName: "\u5f20", firstName: "\u4f1f"}] : plan.parent_metadata.creators,
    getField: field => field === "title" ? (mode === "wrong_title" ? "Another article" : plan.parent_metadata.title)
        : field === "DOI" ? (mode === "wrong_doi" ? "10.1000/another" : plan.parent_metadata.doi)
        : field === "date" ? (mode === "wrong_year" ? "1999" : plan.parent_metadata.year)
        : field === "extra" ? "PMID: " + (mode === "wrong_pmid" ? "87654321" : plan.parent_metadata.pmid || "") : ""
};
const unescape = s => s.replace(/&(amp|lt|gt|quot|#x27|#39);/g, (m, name) => ({amp:"&",lt:"<",gt:">",quot:'"',"#x27":"'","#39":"'"}[name]));
// Minimal DOM model for these generated-note fixtures. Browser tests use real DOMParser.
class DOMParser {
    parseFromString(source) {
        const body = {nodeType: 1, tagName: "BODY", childNodes: []};
        const stack = [body];
        for (const token of source.match(/<!--[^]*?-->|<[^>]*>|[^<]+/g) || []) {
            if (token.startsWith("<!--")) continue;
            const closing = token.match(/^<\/([a-z0-9]+)/i);
            if (closing) {
                if (stack.length > 1) stack.pop();
            } else if (token.startsWith("<")) {
                const tag = token.match(/^<([a-z0-9]+)/i);
                if (!tag) continue;
                const node = {nodeType: 1, tagName: tag[1].toUpperCase(), childNodes: []};
                stack.at(-1).childNodes.push(node);
                if (!["BR", "HR", "IMG", "INPUT", "META"].includes(node.tagName)) stack.push(node);
            } else stack.at(-1).childNodes.push({nodeType: 3, nodeValue: unescape(token)});
        }
        return {body};
    }
}
const Zotero = {
    Prefs: {get: key => preferences.get(key), set: (key, value) => preferences.set(key, value)},
    Utilities: {unescapeHTML: unescape},
    Users: {getCurrentUserID: () => mode === "wrong_library" ? -1 : plan.item.library.id || 1234},
    Groups: {getByGroupID: id => mode === "wrong_group" || id !== plan.item.library.id ? null : {libraryID: internalID}},
    Libraries: {userLibraryID: mode === "wrong_library" ? 99 : internalID, get: () => ({editable: mode !== "readonly"})},
    Collections: {getByLibraryAndKeyAsync: async () => ({id: 11, libraryID: internalID})},
    Items: {
        getByLibraryAndKeyAsync: async (libraryID, key) => libraryID === internalID && key === parent.key ? parent : null,
        getAsync: async id => {
            if (mode === "unreadable" && creates) throw new Error("Read failure");
            return db.get(id);
        }
    },
    Item: class {
        constructor(type) { this.type = type; this.deleted = false; }
        isNote() { return this.type === "note"; }
        setNote(content) { this.content = mode === "inline_html" ? content.replace(/improved/g, "im<em>prov</em>ed") : content; }
        getNote() { return this.content; }
        async saveTx() {
            creates++;
            this.key = "NOTE" + creates;
            this.id = creates + 1;
            if (mode === "wrong_parent") this.parentKey = "OTHER";
            if (mode !== "false_success") db.set(this.id, this);
            if (mode === "lost_reply") throw new Error("Response lost after commit");
            return this.id;
        }
    }
};
const preferences = new Map();
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const execute = new AsyncFunction("Zotero", "DOMParser", script);
(async () => {
    let first, second;
    try {
        first = JSON.parse(await execute(Zotero, DOMParser));
        const repeat = secondScript ? new AsyncFunction("Zotero", "DOMParser", secondScript) : execute;
        second = JSON.parse(await repeat(Zotero, DOMParser));
        process.stdout.write(JSON.stringify({first, second, creates}));
    } catch (e) {
        process.stdout.write(JSON.stringify({first, second, error: String(e), creates}));
    }
})();
