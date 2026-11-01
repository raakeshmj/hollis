import { Link } from "react-router";
import { Empty, PageHead } from "../components/ui";

export function NotFound() {
  return (
    <>
      <PageHead title="Not found" />
      <Empty title="There is nothing at this address." actions={<Link className="btn" to="/">Go to the overview</Link>} />
    </>
  );
}
