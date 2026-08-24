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

    function finiteNumber(value, fallback) {
        var number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function historyPresentation(history, showErrors) {
        if (!Array.isArray(history)) throw new Error("dashboard history must be an array");
        var errorCount = history.filter(function(item) {
            return item && item.verdict === "error";
        }).length;
        return {
            rows: showErrors ? history.slice() : history.filter(function(item) {
                return !item || item.verdict !== "error";
            }),
            errorCount: errorCount
        };
    }

    function taoMarketCapHotkeyUrl(hotkey) {
        var address = String(hotkey || "").trim();
        if (!address) return "";
        return "https://taomarketcap.com/hotkey/" + encodeURIComponent(address) + "/metagraph";
    }

    function shardPresentation(record) {
        var rawGroups = record && Array.isArray(record.shards_used) ? record.shards_used : [];
        var groups = rawGroups.map(function(group) {
            var source = group && typeof group.source === "string" && group.source.trim()
                ? group.source.trim() : "dataset";
            var rawNames = group && Array.isArray(group.names) ? group.names : [];
            var names = [];
            rawNames.forEach(function(value) {
                if (typeof value !== "string" || !value.trim()) return;
                var name = value.trim();
                if (names.indexOf(name) === -1) names.push(name);
            });
            return { source: source, names: names };
        }).filter(function(group) { return group.names.length > 0; });
        return {
            groups: groups,
            count: groups.reduce(function(total, group) { return total + group.names.length; }, 0)
        };
    }

    function decisionPresentation(record) {
        record = record || {};
        var verdict = String(record.verdict || "").toLowerCase();
        var lcb = finiteNumber(record.lcb, null);
        var threshold = finiteNumber(
            record.delta_threshold == null ? record.delta : record.delta_threshold,
            null
        );
        var won = record.accepted === true || verdict === "accepted" || verdict === "challenger";
        var lost = record.accepted === false || verdict === "rejected" || verdict === "king";

        if (verdict === "error") {
            return {
                kind: "error",
                label: "ERROR REASON",
                summary: record.error_message || "The evaluation did not produce a verdict.",
                detail: "No win or loss was recorded."
            };
        }
        if (won) {
            return {
                kind: "win",
                label: "WIN REASON",
                summary: lcb == null || threshold == null
                    ? "The challenger cleared the promotion policy."
                    : "LCB " + lcb.toFixed(6) + " > REQUIRED " + threshold.toFixed(6)
                        + " · MARGIN +" + (lcb - threshold).toFixed(6),
                detail: "The confidence-adjusted improvement was high enough to replace the king."
            };
        }
        if (lost) {
            return {
                kind: "loss",
                label: "LOSS REASON",
                summary: lcb == null || threshold == null
                    ? "The challenger did not clear the promotion policy."
                    : "LCB " + lcb.toFixed(6) + " ≤ REQUIRED " + threshold.toFixed(6)
                        + " · SHORTFALL " + Math.max(0, threshold - lcb).toFixed(6),
                detail: "The measured improvement was not confident enough to replace the king."
            };
        }
        return {
            kind: "unknown",
            label: "DECISION",
            summary: "A final win or loss reason is not available.",
            detail: ""
        };
    }

    function datasetPresentation(manifest) {
        if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
            throw new Error("dataset manifest must be an object");
        }
        if (!Array.isArray(manifest.sources)) {
            throw new Error("dataset manifest sources must be an array");
        }
        var sources = manifest.sources.filter(function(source) {
            return source && source.enabled !== false;
        });
        var rawWeights = sources.map(function(source) {
            return Math.max(0, finiteNumber(
                source.proportion == null ? source.weight : source.proportion,
                0
            ));
        });
        var weightTotal = rawWeights.reduce(function(total, weight) { return total + weight; }, 0);
        var defaultWeight = sources.length ? 1 / sources.length : 0;
        var evalN = Math.max(0, finiteNumber(manifest.eval_n || manifest.default_eval_sequences, 0));
        var rows = sources.map(function(source, index) {
            var weight = weightTotal > 0 ? rawWeights[index] / weightTotal : defaultWeight;
            var sequenceLength = Math.max(0, finiteNumber(
                source.sequence_length || source.seq_len || manifest.sequence_length,
                0
            ));
            var totalTokens = Math.max(0, finiteNumber(source.total_tokens, 0));
            var totalShards = Math.max(0, finiteNumber(
                source.total_shards,
                0
            ));
            var sequences = Math.max(0, finiteNumber(
                source.estimated_sequences,
                sequenceLength ? Math.floor(totalTokens / sequenceLength) : 0
            ));
            var evalSequences = evalN ? Math.round(evalN * weight) : 0;
            var evalTokens = evalSequences * sequenceLength;
            return {
                name: source.name || "dataset",
                manifestUrl: source.manifest_url || "",
                manifestSha256: source.manifest_sha256 || "",
                weight: finiteNumber(source.proportion == null ? source.weight : source.proportion, 0),
                normalizedWeight: weight,
                totalTokens: totalTokens,
                totalShards: totalShards,
                sequences: sequences,
                sequenceLength: sequenceLength,
                evalSequences: evalSequences,
                evalTokens: evalTokens,
                sampleRate: totalTokens ? evalTokens / totalTokens * 100 : 0,
                source: source.source_repo || source.source || "",
                tokenizer: source.tokenizer || "",
                dtype: source.dtype || "",
                tokenizationMode: source.tokenization_mode || "",
                metadataLoaded: Boolean(totalTokens || totalShards || sequenceLength || source.source_repo)
            };
        });
        var totalTokens = rows.reduce(function(total, row) { return total + row.totalTokens; }, 0);
        var totalShards = rows.reduce(function(total, row) { return total + row.totalShards; }, 0);
        var totalSequences = rows.reduce(function(total, row) { return total + row.sequences; }, 0);
        var sequenceLengths = rows.map(function(row) { return row.sequenceLength; }).filter(Boolean);
        var sharedSequenceLength = sequenceLengths.length && sequenceLengths.every(function(value) {
            return value === sequenceLengths[0];
        }) ? sequenceLengths[0] : 0;
        var tokenizers = rows.map(function(row) { return row.tokenizer; }).filter(Boolean);
        var sharedTokenizer = tokenizers.length && tokenizers.every(function(value) {
            return value === tokenizers[0];
        }) ? tokenizers[0] : "";
        return {
            label: manifest.dataset_label || manifest.name || "dataset mix",
            generatedAt: manifest.generated_at || manifest.updated || "",
            evalN: evalN,
            rows: rows,
            totalTokens: totalTokens,
            totalShards: totalShards,
            totalSequences: totalSequences,
            sequenceLength: sharedSequenceLength,
            evalTokens: rows.reduce(function(total, row) { return total + row.evalTokens; }, 0),
            tokenizer: sharedTokenizer
        };
    }

    return {
        HIDDEN: HIDDEN,
        validate: validate,
        presentation: presentation,
        historyPresentation: historyPresentation,
        taoMarketCapHotkeyUrl: taoMarketCapHotkeyUrl,
        shardPresentation: shardPresentation,
        decisionPresentation: decisionPresentation,
        datasetPresentation: datasetPresentation
    };
});
