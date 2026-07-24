// ---------- לוגיקת החנות: קטלוג, פילטרים, עגלה (localStorage) ותהליך הזמנה ----------

const FREE_SHIPPING = 199;
const SHIPPING_FEE = 29;
const CART_KEY = "trendcart:cart";

const money = (n) => "₪" + n.toLocaleString("he-IL", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const products = window.PRODUCTS || [];
const byId = (id) => products.find((p) => p.id === id);

// עגלה נשמרת מקומית כדי לשרוד רענון דף
let cart = loadCart();
let activeCat = "הכל";

function loadCart() {
  try { return JSON.parse(localStorage.getItem(CART_KEY)) || {}; }
  catch { return {}; }
}
function saveCart() {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
}

// ---------- רינדור פילטרים ----------
function renderFilters() {
  const cats = ["הכל", ...new Set(products.map((p) => p.category))];
  const el = document.getElementById("filters");
  el.innerHTML = cats
    .map((c) => `<button class="chip ${c === activeCat ? "active" : ""}" data-cat="${c}">${c}</button>`)
    .join("");
  el.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => { activeCat = chip.dataset.cat; renderFilters(); renderGrid(); });
  });
}

// ---------- רינדור רשת מוצרים ----------
function stars(rating, reviews) {
  const full = Math.round(rating);
  return `<div class="stars">${"★".repeat(full)}${"☆".repeat(5 - full)} <span>(${reviews.toLocaleString("he-IL")})</span></div>`;
}
function renderGrid() {
  const grid = document.getElementById("grid");
  const list = activeCat === "הכל" ? products : products.filter((p) => p.category === activeCat);
  grid.innerHTML = list.map((p) => {
    const off = p.compareAt ? Math.round((1 - p.price / p.compareAt) * 100) : 0;
    return `
    <article class="product">
      <div class="product-img" style="background:linear-gradient(135deg, ${p.color}33, ${p.color}0a);">
        ${p.emoji}
        ${p.badge ? `<span class="product-badge">${p.badge}</span>` : ""}
        ${off ? `<span class="discount-badge">-${off}%</span>` : ""}
      </div>
      <div class="product-body">
        <span class="product-cat">${p.category}</span>
        <div class="product-name">${p.name}</div>
        ${stars(p.rating, p.reviews)}
        <div class="product-desc">${p.desc}</div>
        <div class="price-row">
          <span class="price">${money(p.price)}</span>
          ${p.compareAt ? `<span class="compare">${money(p.compareAt)}</span>` : ""}
        </div>
        <button class="add-btn" data-add="${p.id}">הוספה לעגלה 🛒</button>
      </div>
    </article>`;
  }).join("");
  grid.querySelectorAll("[data-add]").forEach((b) =>
    b.addEventListener("click", () => addToCart(b.dataset.add))
  );
}

// ---------- פעולות עגלה ----------
function addToCart(id) {
  cart[id] = (cart[id] || 0) + 1;
  saveCart();
  updateCartUI();
  toast("נוסף לעגלה ✓");
}
function setQty(id, qty) {
  if (qty <= 0) delete cart[id];
  else cart[id] = qty;
  saveCart();
  updateCartUI();
}
function cartEntries() {
  return Object.entries(cart).map(([id, qty]) => ({ product: byId(id), qty })).filter((e) => e.product);
}
function cartSubtotal() {
  return cartEntries().reduce((sum, e) => sum + e.product.price * e.qty, 0);
}
function cartCount() {
  return Object.values(cart).reduce((a, b) => a + b, 0);
}

function updateCartUI() {
  document.getElementById("cartCount").textContent = cartCount();
  const body = document.getElementById("cartBody");
  const foot = document.getElementById("cartFoot");
  const entries = cartEntries();

  if (entries.length === 0) {
    body.innerHTML = `<div class="empty-cart">🛒<br/><br/>העגלה ריקה עדיין.<br/>הוסיפו מוצרים כדי להתחיל!</div>`;
    foot.style.display = "none";
    return;
  }

  body.innerHTML = entries.map((e) => `
    <div class="cart-item">
      <div class="thumb" style="background:${e.product.color}22;">${e.product.emoji}</div>
      <div class="info">
        <b>${e.product.name}</b>
        <span>${money(e.product.price)}</span>
        <div class="qty">
          <button data-dec="${e.product.id}">−</button>
          <span>${e.qty}</span>
          <button data-inc="${e.product.id}">+</button>
        </div>
        <button class="remove" data-rm="${e.product.id}">הסרה</button>
      </div>
      <div style="font-weight:700;">${money(e.product.price * e.qty)}</div>
    </div>`).join("");

  body.querySelectorAll("[data-inc]").forEach((b) => b.addEventListener("click", () => setQty(b.dataset.inc, cart[b.dataset.inc] + 1)));
  body.querySelectorAll("[data-dec]").forEach((b) => b.addEventListener("click", () => setQty(b.dataset.dec, cart[b.dataset.dec] - 1)));
  body.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => setQty(b.dataset.rm, 0)));

  const subtotal = cartSubtotal();
  const shipping = subtotal >= FREE_SHIPPING || subtotal === 0 ? 0 : SHIPPING_FEE;
  const total = subtotal + shipping;
  document.getElementById("cartTotal").textContent = money(total);
  document.getElementById("modalTotal").textContent = money(total);

  const note = document.getElementById("shipNote");
  if (shipping === 0) {
    note.textContent = "🎉 זכית במשלוח חינם!";
  } else {
    const left = FREE_SHIPPING - subtotal;
    note.textContent = `עוד ${money(left)} למשלוח חינם · דמי משלוח: ${money(SHIPPING_FEE)}`;
  }
  foot.style.display = "block";
}

// ---------- מגירת עגלה ----------
const overlay = document.getElementById("overlay");
const drawer = document.getElementById("drawer");
function openDrawer() { overlay.classList.add("open"); drawer.classList.add("open"); }
function closeDrawer() { overlay.classList.remove("open"); drawer.classList.remove("open"); }
document.getElementById("openCart").addEventListener("click", openDrawer);
document.getElementById("closeCart").addEventListener("click", closeDrawer);
overlay.addEventListener("click", closeDrawer);

// ---------- ניווט לפי קטגוריה מהתפריט ----------
document.querySelectorAll("[data-cat]").forEach((a) => {
  if (a.classList.contains("chip")) return;
  a.addEventListener("click", (e) => {
    e.preventDefault();
    activeCat = a.dataset.cat;
    renderFilters(); renderGrid();
    document.getElementById("products").scrollIntoView({ behavior: "smooth" });
  });
});

// ---------- תהליך תשלום ----------
const modal = document.getElementById("checkoutModal");
document.getElementById("checkoutBtn").addEventListener("click", () => {
  closeDrawer();
  modal.classList.add("open");
});
document.getElementById("cancelCheckout").addEventListener("click", () => {
  modal.classList.remove("open");
  openDrawer();
});
modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });

document.getElementById("checkoutForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target).entries());
  const orderId = "TC-" + Date.now().toString().slice(-6);
  const items = cartEntries();
  // כאן בפרודקשן שולחים את ההזמנה ל-backend / ספק הדרופשיפינג / מערכת סליקה.
  console.log("ORDER", { orderId, customer: data, items, total: cartSubtotal() });

  document.getElementById("checkoutInner").innerHTML = `
    <div class="success">
      <div class="check">✅</div>
      <h3>ההזמנה התקבלה!</h3>
      <p class="sub">מספר הזמנה: <b>${orderId}</b></p>
      <p style="color:var(--muted);">תודה ${data.name.split(" ")[0]}! שלחנו אישור למייל ${data.email}.<br/>נציג יצור איתך קשר לתיאום המשלוח.</p>
      <button class="btn btn-primary" style="width:100%;margin-top:16px;" onclick="location.reload()">חזרה לחנות</button>
    </div>`;

  cart = {};
  saveCart();
  updateCartUI();
});

// ---------- Toast ----------
let toastTimer;
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 1800);
}

// ---------- אתחול ----------
renderFilters();
renderGrid();
updateCartUI();
