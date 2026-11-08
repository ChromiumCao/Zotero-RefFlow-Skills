// Reviewed membership removal only. Zotero Tools > Developer > Run JavaScript, async mode.
const plan = __PLAN_JSON__;
return await (async () => {
    const lib = plan.library;
    let libraryID;
    if (lib.type === "user") {
        if (lib.id !== 0 && lib.id !== Zotero.Users.getCurrentUserID()) throw new Error("User library mismatch");
        libraryID = Zotero.Libraries.userLibraryID;
    } else {
        const group = Zotero.Groups.getByGroupID(lib.id);
        if (!group) throw new Error("Group not found");
        libraryID = group.libraryID;
    }
    if (!Zotero.Libraries.get(libraryID).editable) throw new Error("Library is read-only");
    const collection = await Zotero.Collections.getByLibraryAndKeyAsync(libraryID, plan.target_collection);
    if (!collection || collection.libraryID !== libraryID) throw new Error("Target collection mismatch");
    const rows = [];
    const keys = item => item.getCollections().map(id => Zotero.Collections.get(id).key);
    for (const candidate of plan.candidates) {
        const item = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, candidate.item.item_key);
        if (!item || item.deleted || !item.isRegularItem() || item.libraryID !== libraryID) throw new Error("Reference identity changed");
        if (candidate.item.library.type !== lib.type || candidate.item.library.id !== lib.id) throw new Error("Cross-library candidate");
        const before = keys(item);
        const old = candidate.data.collections || [];
        if (old.some(key => key !== plan.target_collection && !before.includes(key))) throw new Error("Other memberships changed");
        if (before.includes(plan.target_collection)) {
            if (candidate.version !== null && candidate.version !== undefined && item.version !== candidate.version) throw new Error("Reference version changed");
            if (item.getField("title") !== candidate.data.title || String(item.getField("DOI") || "") !== String(candidate.data.DOI || "")) throw new Error("Reference metadata changed");
            if (before.length !== old.length || before.some(key => !old.includes(key))) throw new Error("Membership changed since preview");
        }
        rows.push({candidate, item});
    }
    const results = [];
    for (const {candidate, item} of rows) {
        let error = null;
        if (item.getCollections().includes(collection.id)) {
            try { await collection.removeItems([item.id]); }
            catch (e) { error = String(e); }
        }
        const current = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, item.key);
        const members = current && !current.deleted ? keys(current) : [];
        const verified = !!current && !current.deleted && !members.includes(plan.target_collection)
            && (candidate.data.collections || []).every(key => key === plan.target_collection || members.includes(key));
        results.push({item: candidate.item, membership_removed_verified: verified, outcome: verified ? "removed" : "pending", error});
        if (error && !verified) break;
    }
    return JSON.stringify({schema_version: "zotero-input.removal-results/1", items: results,
                           complete: results.length === rows.length && results.every(row => row.membership_removed_verified)});
})();
