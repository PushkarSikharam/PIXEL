import { redirect } from "next/navigation";

// The first screen for a signed-out person is sign-in; for a signed-in person, the console.
export default function Home() {
  redirect("/sign-in");
}
