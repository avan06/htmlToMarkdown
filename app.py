import gradio as gr
import os
import shutil
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from queue import Queue
import time
import zipfile
import tempfile
import sys
import logging
import traceback
import pypandoc

# --- Configuration & Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36'
REQUEST_TIMEOUT = 20 # seconds
POLITENESS_DELAY = 0.3 # seconds between requests

# --- Pandoc Check ---
def check_pandoc_available():
    """Checks if pypandoc can find a pandoc executable."""
    try:
        pandoc_path = pypandoc.get_pandoc_path()
        logging.info(f"pypandoc found Pandoc executable at: {pandoc_path}")
        return True
    except OSError:
        logging.error("pypandoc could not find Pandoc executable.")
        logging.error("Please ensure Pandoc is installed OR install 'pypandoc_binary' (`pip install pypandoc_binary`)")
        return False
    except ImportError:
        logging.error("pypandoc library not found. Please install it (`pip install pypandoc_binary`).")
        return False

# --- Core Functions ---
def fetch_html(url):
    """Fetches HTML content from a given URL."""
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        response.raise_for_status()
        response.encoding = response.apparent_encoding if response.apparent_encoding else 'utf-8'
        logging.info(f"Successfully fetched: {url}")
        return response.text
    except requests.exceptions.Timeout:
        logging.error(f"Timeout fetching URL: {url}")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching URL {url}: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error fetching {url}: {e}")
        return None

def convert_html_to_md(html_content, output_md_path, pandoc_output_format, pandoc_extra_args):
    """
    Converts HTML content string to a Markdown file using pypandoc
    with specified format and arguments.
    """
    if not html_content:
        logging.warning(f"Empty HTML content for {output_md_path}. Conversion skipped.")
        return False
    # Using html+smart enables better handling of typographic characters in source HTML
    input_format = 'html+smart' # Keep input format consistent

    try:
        logging.debug(f"pypandoc converting to {pandoc_output_format} with args: {pandoc_extra_args}")
        # Use pypandoc.convert_text to convert the HTML string
        # Specify input format ('html'), output format ('gfm'), and output file
        # pypandoc handles invoking pandoc correctly with the string input
        output = pypandoc.convert_text(
            source=html_content,
            to=pandoc_output_format,
            format=input_format,
            outputfile=output_md_path,
            extra_args=pandoc_extra_args,
            encoding='utf-8'
        )

        # When using outputfile, convert_text returns an empty string on success
        if output == "":
            logging.info(f"Successfully converted using pypandoc -> {os.path.basename(output_md_path)}")
            return True
        else:
            logging.error(f"pypandoc conversion to {output_md_path} returned unexpected non-empty output.")
            if os.path.exists(output_md_path) and os.path.getsize(output_md_path) == 0:
                 logging.warning(f"Output file {output_md_path} was created but is empty.")
            return False

    except Exception as e:
        logging.error(f"Error during pypandoc conversion for {output_md_path}: {e}")
        logging.error(traceback.format_exc())
        if os.path.exists(output_md_path) and os.path.getsize(output_md_path) == 0:
            try:
                os.remove(output_md_path)
                logging.info(f"Removed empty/failed output file: {os.path.basename(output_md_path)}")
            except OSError as remove_err:
                logging.warning(f"Could not remove empty/failed output file {output_md_path}: {remove_err}")
        return False


def create_zip_archive(source_dir, output_zip_path):
    """Creates a ZIP archive from the contents of source_dir."""
    try:
        with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Arcname is the path inside the zip file (relative to source_dir)
                    arcname = os.path.relpath(file_path, source_dir)
                    zipf.write(file_path, arcname)
        logging.info(f"Successfully created ZIP archive: {output_zip_path}")
        return True
    except Exception as e:
        logging.error(f"Failed to create ZIP archive {output_zip_path}: {e}")
        return False
      
# --- Main Gradio Function ---
def process_conversion_request(start_url_str, restrict_path, use_aggressive_conversion, progress=gr.Progress(track_tqdm=True)):
    """The main function triggered by the Gradio interface."""

    # --- 0. Check Pandoc via pypandoc ---
    if not check_pandoc_available():
         return "Error: pypandoc could not find a Pandoc executable. Please ensure Pandoc is installed or install `pypandoc_binary`.", None

    # --- 1. Validate URL and Determine Restriction Path ---
    start_url_str = start_url_str.strip()
    start_path_dir_for_restriction = None # Initialize restriction path base

    if not start_url_str:
        return "Error: Starting URL cannot be empty.", None
    try:
        parsed_start_url = urlparse(start_url_str)
        if not parsed_start_url.scheme or not parsed_start_url.netloc:
            raise ValueError("Invalid URL format (missing scheme or domain).")
        base_netloc = parsed_start_url.netloc
        base_scheme = parsed_start_url.scheme

        # Calculate the base directory path for comparison if restriction is enabled
        start_path_cleaned = parsed_start_url.path.strip('/')
        if start_path_cleaned: # If not root path
            # Use os.path.dirname to get the directory part
            # dirname('main/index.html') -> 'main'
            # dirname('main') -> '' (This needs correction if start URL is like /main/)
            # Let's adjust: if no '/' it means it's the first level dir or a root file
            if '/' not in start_path_cleaned and '.' not in start_path_cleaned:
                 start_path_dir_for_restriction = start_path_cleaned # e.g. 'main'
            else:
                 start_path_dir_for_restriction = os.path.dirname(start_path_cleaned) # e.g. 'main' from main/index.html, or '' from /index.html
                 if start_path_dir_for_restriction == '': # Handle case like /index.html correctly
                     start_path_dir_for_restriction = None # Treat like root, don't restrict path based on this

    except ValueError as e:
        return f"Error: Invalid starting URL '{start_url_str}': {e}", None

    # Log restriction status
    restriction_msg = f"Path restriction enabled: limiting to paths starting like '{start_path_dir_for_restriction}/'." if restrict_path and start_path_dir_for_restriction else "Path restriction disabled or starting from root."

    # --- Determine Pandoc Settings based on Checkbox ---
    # wrap=none, Prevent auto-wrapping lines
    if use_aggressive_conversion:
        pandoc_format_to_use = 'gfm-raw_html+hard_line_breaks'
        pandoc_args_to_use = ['--wrap=none', '--markdown-headings=atx']
        conversion_mode_msg = "Using aggressive Markdown conversion (less raw HTML, ATX headers)."
    else:
        # Using gfm+hard_line_breaks ensures GitHub compatibility and respects single newlines
        pandoc_format_to_use = 'gfm+hard_line_breaks'
        pandoc_args_to_use = ['--wrap=none']
        conversion_mode_msg = "Using standard Markdown conversion (may preserve more raw HTML)."

    logging.info(conversion_mode_msg) # Log the mode

    # --- 2. Setup Temporary Directory & Crawler ---
    staging_dir = tempfile.mkdtemp(prefix="md_convert_")
    logging.info(f"Created temporary staging directory: {staging_dir}")
    output_zip_file = None

    urls_to_process = Queue()
    processed_urls = set() # Still needed to avoid duplicates
    failed_urls = set()
    converted_count = 0
    url_count_estimate = 1 # Total unique URLs discovered so far (starts with the first one)
    dequeued_count = 0

    urls_to_process.put(start_url_str)
    processed_urls.add(start_url_str) # Add start URL here

    log_messages = ["Process started...", restriction_msg, conversion_mode_msg]

    try:
        # --- 3. Crawl and Convert Loop ---
        while not urls_to_process.empty():
            # --- Get URL and Increment Dequeued Count ---
            current_url = urls_to_process.get()
            dequeued_count += 1 # Increment when an item is taken for processing

            # --- Update Progress Bar ---
            # Calculate progress based on dequeued items vs. total discovered
            # Denominator is the total number of unique URLs added to processed_urls/queue so far
            denominator = max(1, url_count_estimate) # url_count_estimate increases when new links are found
            current_progress_value = dequeued_count / denominator

            # Update Gradio progress - use dequeued_count for user display
            # Display: Processed X / Total_Discovered Y
            progress(current_progress_value, desc=f"Processing {dequeued_count}/{url_count_estimate}. Queue: {urls_to_process.qsize()}")

            # --- Process the current URL ---
            log_message = f"\nProcessing ({dequeued_count}/{url_count_estimate}): {current_url}"
            logging.info(log_message)
            log_messages.append(log_message)

            # --- 3a. Fetch HTML ---
            time.sleep(POLITENESS_DELAY)
            html_content = fetch_html(current_url)
            if not html_content:
                failed_urls.add(current_url)
                log_message = f"  -> Failed to fetch content."
                logging.warning(log_message)
                log_messages.append(log_message)
                continue

            # --- 3b. Determine Output Path ---
            parsed_current_url = urlparse(current_url)
            # Get the path part of the URL, removing leading/trailing slashes
            url_path_segment = parsed_current_url.path.strip('/') # e.g., "main/index.html", "HEAD/index.html", ""
            # If the path is empty (domain root like https://example.com/), use 'index' as the base name
            if not url_path_segment:
                path_in_zip_base = 'index'
            else:
                path_in_zip_base = url_path_segment # e.g., "main/index.html", "HEAD/index.html"

            # Now, determine the final .md filename based on the path base
            if path_in_zip_base.lower().endswith('.html'):
                relative_md_filename = os.path.splitext(path_in_zip_base)[0] + ".md"
            elif path_in_zip_base.endswith('/'): # Should not happen often with strip('/') but handle defensively
                # If URL was like /docs/, path_in_zip_base would be 'docs' after strip.
                # This case is less likely needed now, but safe to keep.
                relative_md_filename = os.path.join(path_in_zip_base, "index.md")
            else:
                # If it's not empty and doesn't end with .html, assume it's a directory path
                # Append 'index.md' to treat it like accessing a directory index
                # e.g., if URL path was /main, url_path_segment is 'main', output becomes 'main/index.md'
                # If URL path was /path/to/file (no .html), output becomes 'path/to/file.md' if '.' in basename, else 'path/to/file/index.md'
                basename = os.path.basename(path_in_zip_base)
                if '.' in basename: # Check if it looks like a file without .html extension
                    relative_md_filename = path_in_zip_base + ".md"
                else: # Assume it's a directory reference
                    relative_md_filename = os.path.join(path_in_zip_base, "index.md")
                    
            # Construct full path within the temporary staging directory
            output_md_full_path = os.path.join(staging_dir, relative_md_filename)
            output_md_dir = os.path.dirname(output_md_full_path)

            # Create directories if they don't exist (check if output_md_dir is not empty)
            try:
                if output_md_dir and not os.path.exists(output_md_dir):
                    os.makedirs(output_md_dir)
            except OSError as e:
                log_message = f"  -> Error creating directory {output_md_dir}: {e}. Skipping conversion for this URL."
                logging.error(log_message)
                log_messages.append(log_message)
                failed_urls.add(current_url)
                continue # Skip to next URL

            # --- 3c. Convert HTML to Markdown ---
            if convert_html_to_md(html_content, output_md_full_path, pandoc_format_to_use, pandoc_args_to_use):
                converted_count += 1
                log_message = f"  -> Converted successfully to {os.path.relpath(output_md_full_path, staging_dir)}"
                logging.info(log_message)
                log_messages.append(log_message)
            else:
                failed_urls.add(current_url)
                log_message = f"  -> Conversion failed."
                logging.warning(log_message)
                log_messages.append(log_message)

            # --- 3d. Find and Add New Links ---
            try:
                soup = BeautifulSoup(html_content, 'lxml')
                links_found_this_page = 0
                links_skipped_due_to_path = 0
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    absolute_url = urljoin(current_url, href)
                    absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                    parsed_absolute_url = urlparse(absolute_url)

                    # Basic Filtering (scheme, domain, looks like html)
                    is_valid_target = (
                        parsed_absolute_url.scheme == base_scheme and
                        parsed_absolute_url.netloc == base_netloc and
                        (not parsed_absolute_url.path or
                         parsed_absolute_url.path == '/' or
                         parsed_absolute_url.path.lower().endswith('.html') or
                         '.' not in os.path.basename(parsed_absolute_url.path.rstrip('/')) # Include directory links
                         )
                    )

                    if not is_valid_target:
                        continue # Skip invalid links early

                    # --- Path Restriction Check ---
                    path_restricted = False
                    # Only apply if checkbox is checked AND we derived a non-root restriction path
                    if restrict_path and start_path_dir_for_restriction is not None:
                        candidate_path_clean = parsed_absolute_url.path.strip('/')
                        # Check if the cleaned candidate path starts with the restriction dir + '/'
                        # OR if the candidate path is exactly the restriction dir (e.g. /main matching main)
                        if not (candidate_path_clean.startswith(start_path_dir_for_restriction + '/') or \
                                candidate_path_clean == start_path_dir_for_restriction):
                            path_restricted = True
                            links_skipped_due_to_path += 1
                    # --- End Path Restriction Check ---

                    # Add to queue only if NOT restricted and NOT already processed
                    if not path_restricted and absolute_url not in processed_urls:
                        processed_urls.add(absolute_url) # Add to set immediately
                        urls_to_process.put(absolute_url)
                        links_found_this_page += 1
                        url_count_estimate += 1

                # Log link discovery summary for the page
                log_links_msg = f"  -> Found {links_found_this_page} new link(s) to process."
                if links_skipped_due_to_path > 0:
                    log_links_msg += f" Skipped {links_skipped_due_to_path} link(s) due to path restriction."
                logging.info(log_links_msg)
                log_messages.append(log_links_msg)
            except Exception as e:
                log_message = f"  -> Error parsing links on {current_url}: {e}"
                logging.error(log_message)
                log_messages.append(log_message)

        # --- 4. Create ZIP Archive ---
        progress(1.0, desc="Zipping files...")
        log_messages.append("\nCrawling complete. Creating ZIP file...")
        yield "\n".join(log_messages), None

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp_zip:
            output_zip_path = temp_zip.name

        if create_zip_archive(staging_dir, output_zip_path):
            log_messages.append(f"\nProcess finished successfully!")
            log_messages.append(f"Converted {converted_count} pages using {'aggressive' if use_aggressive_conversion else 'standard'} mode.") # Inform user of mode used
            if failed_urls:
                log_messages.append(f"Failed to process {len(failed_urls)} URLs (check logs).")
            log_messages.append(f"ZIP file ready: {os.path.basename(output_zip_path)}")
            yield "\n".join(log_messages), output_zip_path
        else:
            log_messages.append("\nError: Failed to create the final ZIP archive.")
            yield "\n".join(log_messages), None

    except KeyboardInterrupt:
        log_messages.append("\nProcess interrupted by user.")
        yield "\n".join(log_messages), None
    except Exception as e:
        log_messages.append(f"\nAn unexpected error occurred: {e}")
        logging.error("Unhandled exception in process_conversion_request:")
        logging.error(traceback.format_exc())
        yield "\n".join(log_messages), None
    finally:
        # --- 5. Cleanup ---
        if os.path.exists(staging_dir):
            try:
                shutil.rmtree(staging_dir)
                logging.info(f"Cleaned up temporary directory: {staging_dir}")
            except Exception as e:
                logging.error(f"Error cleaning up temporary directory {staging_dir}: {e}")

css = """
textarea[rows]:not([rows="1"]) {
    overflow-y: auto !important;
    scrollbar-width: thin !important;
}
textarea[rows]:not([rows="1"])::-webkit-scrollbar {
    all: initial !important;
    background: #f1f1f1 !important;
}
textarea[rows]:not([rows="1"])::-webkit-scrollbar-thumb {
    all: initial !important;
    background: #a8a8a8 !important;
}
"""

# --- Gradio UI Definition ---
with gr.Blocks(title="HTML Docs to Markdown Converter", css=css) as demo:
    gr.Markdown(
        """
        # HTML Documentation to Markdown Converter (via pypandoc)
        Enter the starting `index.html` URL of an online documentation site.
        The script will crawl internal HTML links, convert pages to Markdown, and package results into a ZIP file.
        **Requires `pip install pypandoc_binary`**.
        """
    )

    with gr.Row():
        url_input = gr.Textbox(
            label="Starting Index HTML URL",
            placeholder="e.g., https://dghs-imgutils.deepghs.org/main/index.html"
        )

    with gr.Row():
        restrict_path_checkbox = gr.Checkbox(
            label="Restrict crawl to starting path structure (e.g., if start is '/main/index.html', only crawl '/main/...' URLs)",
            value=True # Default to restricting path
        )
        aggressive_md_checkbox = gr.Checkbox(
            label="Aggressive Markdown conversion (disable raw HTML, use ATX headers)",
            value=True # Default to aggressive conversion
        )

    with gr.Row():
        start_button = gr.Button("Start Conversion", variant="primary")

    with gr.Row():
         log_output = gr.Textbox(label="Progress Logs", lines=15, interactive=False, show_copy_button=True)

    with gr.Row():
        zip_output = gr.File(label="Download Markdown ZIP")

    start_button.click(
        fn=process_conversion_request,
        inputs=[url_input, restrict_path_checkbox, aggressive_md_checkbox],
        outputs=[log_output, zip_output],
        show_progress="full"
    )

# --- Launch App ---
if __name__ == "__main__":
    demo.queue()
    demo.launch(inbrowser=True)
