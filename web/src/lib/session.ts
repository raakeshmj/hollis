/** The signed-in principal and what the interface may offer them. The server enforces every
 * permission; these only decide which controls make sense to show. */

import { useSession } from "../api/hooks";
import type { PrincipalView } from "../api/types";

export function useMe(): PrincipalView | undefined {
  return useSession().data?.principal;
}

export const isAdmin = (me: PrincipalView | undefined) => me?.role === "admin";
export const canOperate = (me: PrincipalView | undefined) => me !== undefined && me.role !== "viewer";
export const canApprove = (me: PrincipalView | undefined) => Boolean(me && (me.role === "admin" || me.can_approve));
