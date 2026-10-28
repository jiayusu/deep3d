//! G4 runner: times the production invalidation API over a frozen Feature graph.
//!
//! The fixture, queries and expected sets are read from files prepared outside
//! this program; nothing here derives expectations from the code under test.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::time::Instant;

use clap::Parser;
use core_domain::{DependencyIndex, InvalidationPlan};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

#[derive(Parser)]
#[command(about = "Measure Feature invalidation over a frozen G4 fixture")]
struct Args {
    #[arg(long)]
    graph: PathBuf,
    #[arg(long)]
    queries: PathBuf,
    #[arg(long)]
    oracle: PathBuf,
    #[arg(long)]
    out: PathBuf,
}

#[derive(Deserialize)]
struct Graph {
    nodes: Vec<Node>,
    edges: Vec<(String, String)>,
}

#[derive(Deserialize)]
struct Node {
    id: String,
    uuid: Uuid,
}

#[derive(Deserialize)]
struct Queries {
    warmup_requests: usize,
    queries: Vec<Query>,
}

#[derive(Deserialize)]
struct Query {
    index: usize,
    edit: String,
}

#[derive(Deserialize)]
struct Oracle {
    expected: Vec<Expected>,
}

#[derive(Deserialize)]
struct Expected {
    query: usize,
    dirty: Vec<String>,
    reasons: BTreeMap<String, Vec<String>>,
}

fn read<T: for<'a> Deserialize<'a>>(path: &PathBuf) -> T {
    let bytes = std::fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn nearest_rank_p95(samples: &[u128]) -> u128 {
    let mut sorted = samples.to_vec();
    sorted.sort_unstable();
    sorted[(sorted.len() * 95).div_ceil(100) - 1]
}

fn named(
    plan: &InvalidationPlan,
    names: &BTreeMap<Uuid, String>,
) -> (BTreeSet<String>, BTreeSet<(String, String)>) {
    let dirty = plan.dirty.iter().map(|id| names[id].clone()).collect();
    let reasons = plan
        .reasons
        .iter()
        .flat_map(|(child, inputs)| {
            inputs
                .iter()
                .map(|input| (names[input].clone(), names[child].clone()))
        })
        .collect();
    (dirty, reasons)
}

fn series(samples: &[u128]) -> Value {
    json!({
        "unit": "ns",
        "count": samples.len(),
        "p95_ns": nearest_rank_p95(samples),
        "max_ns": samples.iter().max(),
        "min_ns": samples.iter().min(),
        "samples_ns": samples,
    })
}

fn main() {
    let args = Args::parse();
    if args.out.exists() {
        panic!("Refusing to overwrite {}", args.out.display());
    }
    let graph: Graph = read(&args.graph);
    let queries: Queries = read(&args.queries);
    let oracle: Oracle = read(&args.oracle);
    let ids: BTreeMap<String, Uuid> = graph.nodes.iter().map(|n| (n.id.clone(), n.uuid)).collect();
    let names: BTreeMap<Uuid, String> =
        graph.nodes.iter().map(|n| (n.uuid, n.id.clone())).collect();
    assert_eq!(ids.len(), graph.nodes.len(), "duplicate node ids");
    assert_eq!(names.len(), graph.nodes.len(), "duplicate node uuids");
    let edges: Vec<(Uuid, Uuid)> = graph.edges.iter().map(|(a, b)| (ids[a], ids[b])).collect();
    let expected: BTreeMap<usize, &Expected> =
        oracle.expected.iter().map(|e| (e.query, e)).collect();
    assert_eq!(
        expected.len(),
        queries.queries.len(),
        "oracle does not cover every query"
    );
    for (position, query) in queries.queries.iter().enumerate() {
        assert_eq!(query.index, position, "queries are not in frozen order");
    }
    let seeds: Vec<BTreeSet<Uuid>> = queries
        .queries
        .iter()
        .map(|q| BTreeSet::from([ids[&q.edit]]))
        .collect();

    // Cold index build, reported separately.
    let start = Instant::now();
    let index = DependencyIndex::from_edges(edges.iter().copied());
    let cold_build_ns = start.elapsed().as_nanos();
    assert_eq!(
        index.len(),
        graph.nodes.len(),
        "index does not cover the registered nodes"
    );

    // Warmup on the first requests of the frozen order; not recorded.
    for seed in seeds.iter().take(queries.warmup_requests) {
        std::hint::black_box(DependencyIndex::from_edges(edges.iter().copied()).invalidate(seed));
        std::hint::black_box(index.invalidate(seed));
    }

    // Primary series: the production API rebuilds its index on every edit
    // (`invalidation_plan`), so the rebuild stays inside the timed region.
    let mut rebuild = Vec::with_capacity(seeds.len());
    let mut plans = Vec::with_capacity(seeds.len());
    for seed in &seeds {
        let start = Instant::now();
        let plan = DependencyIndex::from_edges(edges.iter().copied()).invalidate(seed);
        rebuild.push(start.elapsed().as_nanos());
        plans.push(plan);
    }
    // Secondary series: traversal over an already built index.
    let mut reused = Vec::with_capacity(seeds.len());
    for (seed, primary) in seeds.iter().zip(&plans) {
        let start = Instant::now();
        let plan = index.invalidate(seed);
        reused.push(start.elapsed().as_nanos());
        assert_eq!(&plan, primary, "indexed and rebuilt results differ");
    }

    let mut exact = 0;
    let mut mismatches = Vec::new();
    for (query, plan) in queries.queries.iter().zip(&plans) {
        let (dirty, reasons) = named(plan, &names);
        let want = expected[&query.index];
        let want_dirty: BTreeSet<String> = want.dirty.iter().cloned().collect();
        let want_reasons: BTreeSet<(String, String)> = want
            .reasons
            .iter()
            .flat_map(|(child, inputs)| inputs.iter().map(|i| (i.clone(), child.clone())))
            .collect();
        if dirty == want_dirty && reasons == want_reasons {
            exact += 1;
        } else {
            mismatches.push(json!({
                "query": query.index,
                "false_positive": dirty.difference(&want_dirty).collect::<Vec<_>>(),
                "false_negative": want_dirty.difference(&dirty).collect::<Vec<_>>(),
                "reason_false_positive": reasons.difference(&want_reasons).collect::<Vec<_>>(),
                "reason_false_negative": want_reasons.difference(&reasons).collect::<Vec<_>>(),
            }));
        }
    }
    let report = json!({
        "schema_version": "deep3d.g4-measurement/1",
        "api": "core_domain::DependencyIndex::from_edges + invalidate",
        "feature_nodes": graph.nodes.len(),
        "edges": edges.len(),
        "warmup_requests": queries.warmup_requests,
        "measured_requests": plans.len(),
        "exact_result_requests": exact,
        "mismatches": mismatches,
        "cold_index_build_ns": cold_build_ns,
        "quantile_method": "nearest_rank",
        "timed": {"rebuild_per_request": series(&rebuild), "reused_index": series(&reused)},
        "dirty_sizes": plans.iter().map(|p| p.dirty.len()).collect::<Vec<_>>(),
    });
    std::fs::write(&args.out, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    println!(
        "{}",
        json!({"measured": plans.len(), "exact": exact, "p95_ns": nearest_rank_p95(&rebuild), "reused_p95_ns": nearest_rank_p95(&reused)})
    );
    if exact != plans.len() {
        std::process::exit(2);
    }
}
