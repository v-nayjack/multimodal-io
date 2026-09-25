import { Operator, OperatorConfig, registerOperator } from "@fiftyone/operators";
import { PLUGIN } from "./engine";

/**
 * Reloads the App page. Used after uploading into an empty dataset, whose
 * "No samples yet" page only switches to the grid when the page loads. If
 * uploads are still running, the browser asks before leaving.
 */
class ReloadPage extends Operator {
  get config() {
    return new OperatorConfig({
      name: "reload_page",
      label: "Reload page",
      unlisted: true,
    });
  }

  async execute() {
    window.location.reload();
  }
}

registerOperator(ReloadPage, PLUGIN);
