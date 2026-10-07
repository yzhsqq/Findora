// @vitest-environment jsdom
import {act} from "react";
import {createRoot} from "react-dom/client";
import {expect,it,vi} from "vitest";
import MyOrders from "../src/components/MyOrders";
import type {ProductCard} from "../src/types";
it("待购记录显示真实购买按钮，待购买筛选及移除不会调用交易确认", async () => {
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 const pid="05B050F6-9DF5-4488-9218-B1D919650ADE";
 const url=`https://cjdropshipping.com/product/green-sandalwood-hair-comb-p-${pid}.html`;
 const product:ProductCard={product_id:pid,title:"檀木梳",brand:"",category:"Beauty",origin_country:"",price_major:3,currency:"USD",highlights:[],skus:[],score:1,source_platform:"CJdropshipping",source_url:url,source_url_status:"observed",price_text:"US$3.00"};
 let records=[{record_id:"pending-1",product,status:"PENDING_PURCHASE",sku_id:"",quantity:1,created_at:"2026-10-05T00:00:00Z"}];
 const request=vi.fn(async(path:string,method?:string)=>{if(method==="DELETE"){records=[];return {removed:true};}return path==="/purchase-records"?{records,total:records.length}:{orders:[],total:0};});
 const prepare=vi.fn(async()=>true),resolve=vi.fn(async()=>true);
 const btn=(s:string)=>[...host.querySelectorAll('button')].find(b=>b.textContent===s)!;
 try{
  await act(async()=>root.render(<MyOrders request={request} confirmations={[]} busy={false} error={null} onPrepare={prepare} onResolve={resolve} onRefresh={async()=>{}}/>));
  expect(host.textContent).toContain("檀木梳");expect(host.textContent).toContain("尚未在 CJ 下单");
  const link=host.querySelector<HTMLAnchorElement>(".pending-purchase-link")!;
  expect(link.href).toBe(url);expect(link.target).toBe("_blank");expect(link.rel).toBe("noopener noreferrer");
  expect(host.textContent).not.toContain(url);
  await act(async()=>btn("待购买").click());
  expect(request.mock.calls.some(([path])=>path.includes("status=PENDING_PURCHASE"))).toBe(false);
  await act(async()=>btn("移除记录").click());
  expect(request).toHaveBeenCalledWith("/purchase-records/pending-1","DELETE");
  expect(host.textContent).toContain("还没有待购记录");
  expect(prepare).not.toHaveBeenCalled();expect(resolve).not.toHaveBeenCalled();
 }finally{await act(async()=>root.unmount());host.remove();}
});

it("订单筛选、详情、取消先准备确认单，不能直接扣改账本",async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 const request=vi.fn(async(path:string)=>path==="/purchase-records"?{records:[]}:{orders:path.includes("CANCELLED")?[]:[{order_id:"GBX-1",status:"CONFIRMED",currency:"CNY",total_amount_major:129,shipping_address:"测试收货地",created_at:"2026-09-09T00:00:00Z",cancel_reason:null,lines:[{sku_id:"s1",title:"背包",quantity:1,unit_price_major:129}]}],total:1});
 const prepare=vi.fn(async()=>true),resolve=vi.fn(async()=>true);
 const btn=(s:string)=>[...host.querySelectorAll('button')].find(b=>b.textContent===s)!;
 try{
 await act(async()=>root.render(<MyOrders request={request} confirmations={[]} busy={false} error={null} onPrepare={prepare} onResolve={resolve} onRefresh={async()=>{}}/>));
 expect(host.textContent).toContain("GBX-1");
 await act(async()=>btn("订单详情").click());expect(host.textContent).toContain("测试收货地");
 await act(async()=>btn("取消订单").click());
 const input=host.querySelector('input')!;
 await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,'改变计划');input.dispatchEvent(new Event('input',{bubbles:true}));});
 await act(async()=>host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 expect(prepare).toHaveBeenCalledWith('GBX-1','改变计划');expect(resolve).not.toHaveBeenCalled();
 await act(async()=>btn("已取消").click());expect(request.mock.calls.at(-1)![0]).toContain('status=CANCELLED');expect(host.textContent).toContain('暂时没有这类订单');
 }finally{await act(async()=>root.unmount());host.remove();}
});
