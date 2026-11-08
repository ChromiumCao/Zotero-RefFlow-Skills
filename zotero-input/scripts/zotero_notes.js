// Run only after separate notes consent, in Zotero Tools > Developer > Run JavaScript.
// Enable async mode. Uses Zotero's supported data API, never direct SQLite writes.
const job = __JOB_JSON__;
return await (async () => {
    if (!job.consent.notes || !job.consent.token_cost_warning_acknowledged) throw new Error("Notes consent missing");
    const canon = s => String(s || "").normalize("NFKC").replace(/\s+/g, " ").trim();
    const plain = s => {
        const document = new DOMParser().parseFromString(String(s || ""), "text/html");
        const blocks = new Set(["P", "DIV", "BR", "LI", "BLOCKQUOTE", "UL", "OL", "TABLE", "TR", "TD", "TH",
                                "H1", "H2", "H3", "H4", "H5", "H6", "PRE", "HR", "SECTION", "ARTICLE"]);
        const walk = node => {
            if (node.nodeType === 3) return node.nodeValue;
            if (node.nodeType !== 1) return "";
            const text = Array.from(node.childNodes, walk).join("");
            return blocks.has(node.tagName) ? " " + text + " " : text;
        };
        return canon(walk(document.body));
    };
    const semantic = (text, plan) => {
        const prefix = canon(["Citation provenance snapshot", plan.marker,
            "Document: " + (plan.document_title || "Name not supplied"), "Document ID: " + plan.document_id,
            "Document SHA-256: " + plan.document_hash].join(" "));
        if (!text.startsWith(prefix + " Recorded: ")) return text;
        const remainder = text.slice(prefix.length + 11);
        const match = remainder.match(/^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)) (Snapshot only; later manuscript edits are not tracked\..*)$/);
        return match && Number.isFinite(Date.parse(match[1])) ? prefix + " " + match[2] : text;
    };
    const doi = s => canon(s).toLowerCase().replace(/^(?:https?:\/\/(?:dx\.)?doi\.org\/|doi:\s*)/, "");
    const authorVariants = creator => {
        if (!creator) return [];
        const family = creator.lastName || creator.family || creator.name || "";
        const given = creator.firstName || creator.given || "";
        const normalize = value => canon(value).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
        const variants = [normalize(family)];
        if (/[\u3400-\u9fff]/.test(family) && /[\u3400-\u9fff]/.test(given)) variants.push(normalize(family + given));
        return variants.filter(Boolean);
    };
    const checkMetadata = (plan, parent) => {
        const metadata = plan.parent_metadata;
        const titles = [metadata.title, ...(metadata.title_aliases || [])].map(canon);
        const actualDOI = doi(parent.getField("DOI")), wantedDOI = doi(metadata.doi);
        if (wantedDOI && actualDOI && wantedDOI !== actualDOI) throw new Error("Parent DOI conflict");
        if (!titles.includes(canon(parent.getField("title")))) throw new Error("Parent title conflict");
        const pmid = String(parent.getField("extra") || "").match(/(?:^|\n)\s*PMID\s*:\s*(\d+)\b/i);
        if (metadata.pmid && pmid && String(metadata.pmid) !== pmid[1]) throw new Error("Parent PMID conflict");
        const actualYear = String(parent.getField("date") || "").match(/\b\d{4}\b/);
        if (metadata.year && actualYear && String(metadata.year) !== actualYear[0]) throw new Error("Parent year conflict");
        const wantedAuthors = authorVariants((metadata.creators || [])[0]);
        const actualAuthors = authorVariants(parent.getCreators()[0]);
        if (wantedAuthors.length && actualAuthors.length && !wantedAuthors.some(name => actualAuthors.includes(name))) throw new Error("Parent author conflict");
    };
    const preference = "zoteroInput.provenanceNoteAttempts";
    const saved = JSON.parse(Zotero.Prefs.get(preference) || "[]");
    if (!Array.isArray(saved) || saved.some(id => typeof id !== "string")) throw new Error("Invalid native note journal; no write");
    const attempts = new Set(saved);
    const parents = [];
    for (const plan of job.notes) {
        const lib = plan.item.library;
        let libraryID;
        if (lib.type === "user") {
            const current = Zotero.Users.getCurrentUserID();
            if (lib.id !== 0 && lib.id !== current) throw new Error("Personal library identity mismatch");
            libraryID = Zotero.Libraries.userLibraryID;
        } else if (lib.type === "group") {
            const group = Zotero.Groups.getByGroupID(lib.id);
            if (!group) throw new Error("Group not found");
            libraryID = group.libraryID;
        } else throw new Error("Unsupported library");
        const parent = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, plan.item.item_key);
        if (!parent || parent.deleted || !parent.isRegularItem() || parent.libraryID !== libraryID || parent.key !== plan.item.item_key) throw new Error("Regular parent not found");
        if (plan.target_collection.library.type !== lib.type || plan.target_collection.library.id !== lib.id) throw new Error("Collection library mismatch");
        const collection = await Zotero.Collections.getByLibraryAndKeyAsync(libraryID, plan.target_collection.key);
        if (!collection || collection.libraryID !== libraryID || !parent.getCollections().includes(collection.id)) throw new Error("Parent absent from target collection");
        checkMetadata(plan, parent);
        parents.push({plan, parent, libraryID, collectionID: collection.id});
    }
    const find = async (plan, parent) => {
        const matches = [];
        for (const id of parent.getNotes()) {
            const note = await Zotero.Items.getAsync(id);
            if (!note || note.deleted || !note.isNote() || note.parentKey !== parent.key || note.libraryID !== parent.libraryID) continue;
            if (!note.getNote().includes(plan.marker)) continue;
            if (semantic(plain(note.getNote()), plan) !== semantic(plan.expected_text, plan)) throw new Error("Existing marker has different content");
            matches.push(note);
        }
        if (matches.length > 1) throw new Error("Duplicate note markers; no write");
        return matches[0] || null;
    };
    const results = [];
    for (const {plan, parent, libraryID} of parents) {
        let found = null, error = null, outcome = "existing";
        try {
            found = await find(plan, parent);
            if (!found) {
                if (attempts.has(plan.note_id)) throw new Error("Previous uncertain attempt; read-only verification required");
                if (!Zotero.Libraries.get(libraryID).editable) throw new Error("Library is read-only");
                attempts.add(plan.note_id);
                Zotero.Prefs.set(preference, JSON.stringify(Array.from(attempts)));
                const note = new Zotero.Item("note");
                note.libraryID = libraryID;
                note.parentKey = parent.key;
                note.setNote(plan.html);
                try { await note.saveTx(); } catch (e) { error = String(e); }
                found = await find(plan, parent);
                outcome = "created";
            }
        } catch (e) { error = String(e); }
        results.push({note_id: plan.note_id, note_key: found ? found.key : null,
                      outcome: found ? outcome : "pending", membership_verified: !!found, error});
    }
    for (let i = 0; i < parents.length; i++) {
        try {
            if (parents[i].parent.deleted || !parents[i].parent.getCollections().includes(parents[i].collectionID)) throw new Error("Parent membership changed");
            checkMetadata(parents[i].plan, parents[i].parent);
            const checked = await find(parents[i].plan, parents[i].parent);
            if (!checked) results[i].membership_verified = false;
        } catch (e) { results[i].membership_verified = false; results[i].error = String(e); }
        if (!results[i].membership_verified) results[i].outcome = "pending";
    }
    return JSON.stringify({schema_version: "zotero-input.note-results/1", notes: results,
                          complete: results.length > 0 && job.unresolved.length === 0 && results.every(r => r.membership_verified)});
})();
