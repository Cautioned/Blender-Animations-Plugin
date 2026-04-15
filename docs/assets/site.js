const setCurrentYear = (root = document) => {
    root.querySelectorAll("[data-current-year]").forEach((node) => {
        node.textContent = String(new Date().getFullYear());
    });
};

const applyResponseMetadata = (xhr) => {
    if (!xhr?.responseText) {
        return;
    }

    const parsed = new DOMParser().parseFromString(xhr.responseText, "text/html");
    const title = parsed.querySelector("title");
    const description = parsed.querySelector("meta[name='description']")?.getAttribute("content");

    if (title?.textContent) {
        document.title = title.textContent;
    }

    const currentDescription = document.querySelector("meta[name='description']");
    if (currentDescription && description) {
        currentDescription.setAttribute("content", description);
    }
};

const initializePage = (root = document) => {
    setCurrentYear(root);
};

initializePage();

document.body.addEventListener("htmx:afterSwap", (event) => {
    initializePage(event.target);
    applyResponseMetadata(event.detail.xhr);
});