use core_domain::Project;
use uuid::Uuid;

fn main() {
    let project = Project::new_flange("Flange fixture").expect("valid fixture");
    let plan = project.compile(Uuid::new_v4()).expect("valid plan");
    println!("{}", serde_json::to_string_pretty(&plan).expect("JSON"));
}
