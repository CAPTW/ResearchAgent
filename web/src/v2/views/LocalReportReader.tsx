import { Fragment, type ReactNode } from "react";

// Display only the block syntax emitted by LOCAL_EVIDENCE_REPORT_V1. Source
// text remains text: no HTML, images, active links, or remote resource loading.
export function LocalReportReader({content}:{content:string}) {
 const lines=content.split(/\r?\n/);
 const blocks:ReactNode[]=[];
 let i=0;
 while(i<lines.length){
  const start=i;
  const line=lines[i];
  if(!line.trim()){i++;continue;}
  const fence=/^(`{3,})(.*)$/.exec(line);
  if(fence){
   const body:string[]=[];i++;
   while(i<lines.length&&!new RegExp(`^\x60{${fence[1].length},}\\s*$`).test(lines[i]))body.push(lines[i++]);
   if(i<lines.length)i++;
   blocks.push(<details key={start}><summary>보존된 코드·출처 데이터</summary><pre><code>{body.join("\n")}</code></pre></details>);continue;
  }
  const heading=/^(#{1,3}) (.*)$/.exec(line);
  if(heading){
   // The surrounding workspace already supplies h1–h3.
   const Tag=heading[1].length===1?"h4":heading[1].length===2?"h5":"h6";
   blocks.push(<Tag key={start}>{heading[2]}</Tag>);i++;continue;
  }
  if(line.startsWith("> ")){
   const quote:string[]=[];
   while(i<lines.length&&lines[i].startsWith("> "))quote.push(lines[i++].slice(2));
   blocks.push(<blockquote key={start}>{quote.join("\n")}</blockquote>);continue;
  }
  if(line.startsWith("- ")){
   const items:string[]=[];
   while(i<lines.length&&lines[i].startsWith("- "))items.push(lines[i++].slice(2));
   blocks.push(<ul key={start}>{items.map((value,index)=><li key={index}>{inline(value)}</li>)}</ul>);continue;
  }
  const paragraph=[line];i++;
  while(i<lines.length&&lines[i].trim()&&!/^(#{1,3} |>|- |`{3})/.test(lines[i]))paragraph.push(lines[i++]);
  blocks.push(<p key={start}>{inline(paragraph.join("\n"))}</p>);
 }
 return <article className="local-report-reader" aria-label="로컬 근거 보고서 읽기">{blocks}</article>;
}

function inline(text:string) {
 return text.split(/(`[^`\n]+`)/g).map((part,index)=>part.startsWith("`")&&part.endsWith("`")&&part.length>2
  ?<code key={index}>{part.slice(1,-1)}</code>:<Fragment key={index}>{part}</Fragment>);
}
