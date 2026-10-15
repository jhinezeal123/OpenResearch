export const id = "dsh" as const;
export const label = "DSH";
export function DshLogo({size=16}: {size?:number}) {
 return <svg className="block shrink-0" width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 4h7a8 8 0 0 1 0 16H4V4Zm4 4v8h3a4 4 0 0 0 0-8H8Z" /></svg>;
}
