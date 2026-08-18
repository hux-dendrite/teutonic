(function(root, factory) {
    var api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    root.TeutonicDashboardV1 = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function() {
    "use strict";

    var HIDDEN = "Hidden until promotion";

    function walkFinite(value, path) {
        if (typeof value === "number" && !Number.isFinite(value)) {
            throw new Error("non-finite dashboard number at " + path);
        }
        if (Array.isArray(value)) {
            value.forEach(function(item, index) { walkFinite(item, path + "[" + index + "]"); });
        } else if (value && typeof value === "object") {
            Object.keys(value).forEach(function(key) { walkFinite(value[key], path + "." + key); });
        }
    }

    function validate(payload) {
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            throw new Error("dashboard payload must be an object");
        }
        if (payload.schema_version !== 1) throw new Error("unsupported dashboard schema");
        ["queue", "history", "king_chain"].forEach(function(name) {
            if (!Array.isArray(payload[name])) throw new Error("dashboard " + name + " must be an array");
        });
        ["chain", "stats", "king_payout", "weight_status", "service_status"].forEach(function(name) {
            if (!payload[name] || typeof payload[name] !== "object" || Array.isArray(payload[name])) {
                throw new Error("dashboard " + name + " must be an object");
            }
        });
        walkFinite(payload, "$");
        return payload;
    }

    function identityLabel(record) {
        return record && record.model_identity === "hidden_until_promotion" ? HIDDEN : null;
    }

    function presentation(payload) {
        validate(payload);
        var current = payload.current_eval;
        var queued = payload.queue.length ? payload.queue[0] : null;
        var services = payload.service_status;
        return {
            idle: !current && !queued,
            duelIdentity: identityLabel(current || queued),
            queueIdentity: payload.queue.map(function(item) { return identityLabel(item); }),
            historyIdentity: payload.history.map(function(item) {
                return identityLabel(item) || item.challenger_repo || "Public model";
            }),
            historyEmpty: payload.history.length === 0,
            queueEmpty: payload.queue.length === 0,
            health: services.overall,
            degraded: services.overall !== "healthy",
            marketStale: !!(payload.market && payload.market.stale),
            marketAvailable: !!payload.market
        };
    }

    return { HIDDEN: HIDDEN, validate: validate, presentation: presentation };
});
