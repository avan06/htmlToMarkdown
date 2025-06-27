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

# --- Function for direct HTML to Markdown conversion ---
def convert_html_text_to_md_string(html_content, pandoc_output_format, pandoc_extra_args):
    """
    Converts an HTML string directly to a Markdown string using pypandoc.
    """
    if not html_content or not html_content.strip():
        logging.warning("Input HTML content is empty. Conversion skipped.")
        return None, "Error: HTML content cannot be empty."

    input_format = 'html+smart'
    try:
        logging.debug(f"pypandoc converting text to {pandoc_output_format} with args: {pandoc_extra_args}")
        output_md = pypandoc.convert_text(
            source=html_content,
            to=pandoc_output_format,
            format=input_format,
            extra_args=pandoc_extra_args,
            encoding='utf-8'
        )
        logging.info("Successfully converted HTML text to Markdown string.")
        return output_md, "Conversion successful."
    except Exception as e:
        error_msg = f"Error during pypandoc conversion: {e}"
        logging.error(error_msg)
        logging.error(traceback.format_exc())
        return None, error_msg

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

# --- Main Gradio Function (handles both modes) ---
# The function now handles both URL and direct HTML text input.
# It needs to be a generator (`yield`) to support progress updates in URL mode.
def process_conversion_request(
    input_type, start_url_str, html_text_input,
    restrict_path, use_aggressive_conversion,
    progress=gr.Progress(track_tqdm=True)
):
    """The main function triggered by the Gradio interface, handling both modes."""
    # --- 0. Check Pandoc Availability ---
    if not check_pandoc_available():
        error_msg = "Error: Pandoc executable not found. Please ensure Pandoc is installed or run `pip install pypandoc_binary`."
        # Yield a final state for all outputs
        yield error_msg, None, gr.Markdown(visible=False), None
        return

    # --- Determine Pandoc Settings based on Checkbox ---
    # wrap=none, Prevent auto-wrapping lines
    if use_aggressive_conversion:
        pandoc_format_to_use = 'gfm-raw_html+hard_line_breaks'
        pandoc_args_to_use = ['--wrap=none', '--markdown-headings=atx']
        conversion_mode_msg = "Using aggressive conversion mode (disabling raw HTML, using ATX headers)."
    else:
        # Using gfm+hard_line_breaks ensures GitHub compatibility and respects single newlines
        pandoc_format_to_use = 'gfm+hard_line_breaks'
        pandoc_args_to_use = ['--wrap=none']
        conversion_mode_msg = "Using standard conversion mode (may preserve more raw HTML)."
    logging.info(conversion_mode_msg) # Log the mode

    # --- MODE 1: Convert from URL ---
    if input_type == "Convert from URL":
        staging_dir = None # Initialize to ensure it exists for the finally block
        try:
            # --- 1. Validate URL and Determine Restriction Path ---
            start_url_str = start_url_str.strip()
            if not start_url_str:
                yield "Error: Starting URL cannot be empty.", None, gr.Markdown(visible=False), None
                return

            try:
                parsed_start_url = urlparse(start_url_str)
                if not parsed_start_url.scheme or not parsed_start_url.netloc:
                    raise ValueError("Invalid URL format (missing scheme or domain).")
                base_netloc = parsed_start_url.netloc
                base_scheme = parsed_start_url.scheme

                # Calculate the base directory path for comparison if restriction is enabled
                start_path_cleaned = parsed_start_url.path.strip('/')
                start_path_dir_for_restriction = None # Initialize restriction path base
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
                yield f"Error: Invalid starting URL '{start_url_str}': {e}", None, gr.Markdown(visible=False), None
                return
            
            # Log restriction status
            restriction_msg = f"Path restriction enabled: limiting to paths starting like '{start_path_dir_for_restriction}/'." if restrict_path and start_path_dir_for_restriction else "Path restriction disabled or starting from root."

            # --- 2. Setup Temporary Directory & Crawler ---
            staging_dir = tempfile.mkdtemp(prefix="md_convert_")
            logging.info(f"Created temporary directory: {staging_dir}")
            
            urls_to_process = Queue()
            processed_urls = set() # Still needed to avoid duplicates
            urls_to_process.put(start_url_str)
            processed_urls.add(start_url_str) # Add start URL here
            failed_urls = set()
            converted_count = 0
            url_count_estimate = 1 # Total unique URLs discovered so far (starts with the first one)
            dequeued_count = 0
            
            log_messages = ["Process started...", restriction_msg, conversion_mode_msg]
            yield "\n".join(log_messages), None, gr.Markdown(visible=False), None

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
                
                # Fetch HTML
                time.sleep(POLITENESS_DELAY)
                html_content = fetch_html(current_url)
                if not html_content:
                    failed_urls.add(current_url)
                    log_message = f"  -> Failed to fetch content."
                    logging.warning(log_message)
                    log_messages.append(log_message)
                    continue
                
                # Determine Output Path
                parsed_current_url = urlparse(current_url)
                # Get the path part of the URL, removing leading/trailing slashes
                url_path_segment = parsed_current_url.path.strip('/') or 'index' # e.g., "main/index.html", "HEAD/index.html", ""
                
                # Now, determine the final .md filename based on the path base
                if url_path_segment.lower().endswith('.html'):
                    relative_md_filename = os.path.splitext(url_path_segment)[0] + ".md"
                else:
                    # If it's not empty and doesn't end with .html, assume it's a directory path
                    # Append 'index.md' to treat it like accessing a directory index
                    # e.g., if URL path was /main, url_path_segment is 'main', output becomes 'main/index.md'
                    # If URL path was /path/to/file (no .html), output becomes 'path/to/file.md' if '.' in basename, else 'path/to/file/index.md'
                    basename = os.path.basename(url_path_segment)
                    if '.' in basename: # Check if it looks like a file without .html extension
                        relative_md_filename = url_path_segment + ".md"
                    else: # Assume it's a directory reference
                        relative_md_filename = os.path.join(url_path_segment, "index.md")
                
                # Construct full path within the temporary staging directory
                output_md_full_path = os.path.join(staging_dir, relative_md_filename)
                os.makedirs(os.path.dirname(output_md_full_path), exist_ok=True)
                
                # Convert HTML to Markdown
                if convert_html_to_md(html_content, output_md_full_path, pandoc_format_to_use, pandoc_args_to_use):
                    converted_count += 1
                    log_messages.append(f"  -> Converted successfully to {os.path.relpath(output_md_full_path, staging_dir)}")
                else:
                    failed_urls.add(current_url)
                    log_messages.append("  -> Conversion failed.")

                # Find and Add New Links
                soup = BeautifulSoup(html_content, 'lxml')
                for link in soup.find_all('a', href=True):
                    absolute_url = urljoin(current_url, link['href']).split('#', 1)[0]
                    parsed_absolute_url = urlparse(absolute_url)
                    
                    # Basic Filtering (scheme, domain, looks like html)
                    is_valid_target = (
                        parsed_absolute_url.scheme == base_scheme and
                        parsed_absolute_url.netloc == base_netloc)

                    if not is_valid_target: continue # Skip invalid links early
                    
                    # --- Path Restriction Check ---
                    path_restricted = False
                    # Only apply if checkbox is checked AND we derived a non-root restriction path
                    if restrict_path and start_path_dir_for_restriction:
                        candidate_path = parsed_absolute_url.path.strip('/')
                        # Check if the cleaned candidate path starts with the restriction dir + '/'
                        # OR if the candidate path is exactly the restriction dir (e.g. /main matching main)
                        if not (candidate_path.startswith(start_path_dir_for_restriction + '/') or candidate_path == start_path_dir_for_restriction):
                            path_restricted = True
                    # --- End Path Restriction Check ---
                    
                    # Add to queue only if NOT restricted and NOT already processed
                    if not path_restricted and absolute_url not in processed_urls:
                        processed_urls.add(absolute_url) # Add to set immediately
                        urls_to_process.put(absolute_url)
                        url_count_estimate += 1

            # --- 4. Create ZIP Archive ---
            progress(1.0, desc="Zipping files...")
            log_messages.append("\nCrawling complete. Creating ZIP file...")
            yield "\n".join(log_messages), None, gr.Markdown(visible=False), None

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp_zip:
                output_zip_path = temp_zip.name

            if create_zip_archive(staging_dir, output_zip_path):
                log_messages.append(f"\nProcess finished successfully!")
                log_messages.append(f"Converted {converted_count} pages using {'aggressive' if use_aggressive_conversion else 'standard'} mode.")
                if failed_urls:
                    log_messages.append(f"Failed to process {len(failed_urls)} URLs.")
                yield "\n".join(log_messages), output_zip_path, gr.Markdown(visible=False), None
            else:
                log_messages.append("\nError: Failed to create the final ZIP archive.")
                yield "\n".join(log_messages), None, gr.Markdown(visible=False), None

        except Exception as e:
            error_log = f"\nAn unexpected error occurred: {e}\n{traceback.format_exc()}"
            logging.error(error_log)
            yield error_log, None, gr.Markdown(visible=False), None
        finally:
            # --- Cleanup ---
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir)
                logging.info(f"Cleaned up temporary directory: {staging_dir}")

    # --- MODE 2: Convert from HTML Text ---
    elif input_type == "Convert from HTML Text":
        log_messages = [f"Process started...", conversion_mode_msg]
        
        if not html_text_input or not html_text_input.strip():
            log_messages.append("Error: HTML content cannot be empty.")
            yield "\n".join(log_messages), None, gr.Markdown(visible=False), None
            return

        progress(0.5, desc="Converting HTML text...")
        
        # Use the dedicated string conversion function
        markdown_output, status_msg = convert_html_text_to_md_string(
            html_text_input, pandoc_format_to_use, pandoc_args_to_use
        )
        
        log_messages.append(status_msg)
        progress(1.0, desc="Complete")

        if markdown_output is not None:
            # Create a temporary file for download
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix=".md", delete=False) as temp_md:
                temp_md.write(markdown_output)
                temp_md_path = temp_md.name
            
            log_messages.append("\nMarkdown has been generated. You can preview it below or download the file.")

            # Yield the final state: update logs, clear zip, show markdown preview, provide md file
            yield ("\n".join(log_messages), 
                   None, 
                   gr.Markdown(value=markdown_output, visible=True), 
                   temp_md_path)
        else:
            # Conversion failed, show logs and hide/clear other outputs
            yield ("\n".join(log_messages), 
                   None, 
                   gr.Markdown(visible=False), 
                   None)

css = """
textarea[rows]:not([rows="1"]) {
    height: 250px; /* Give the HTML input box a fixed height */
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
with gr.Blocks(title="HTML to Markdown Converter", css=css) as demo:
    gr.Markdown(
        """
        # HTML to Markdown Converter (via pypandoc)
        Choose an input method:
        1.  **Convert from URL**: Enter the starting `index.html` URL of an online documentation site. The script will crawl internal links, convert pages to Markdown, and package the results into a ZIP file.
        2.  **Convert from HTML Text**: Paste raw HTML source code directly to convert it into a single Markdown output.
        
        **This tool requires `pip install pypandoc_binary` to function correctly.**
        """
    )

    # --- Input type selector ---
    input_type_radio = gr.Radio(
        ["Convert from URL", "Convert from HTML Text"],
        label="Input Type",
        value="Convert from URL"
    )

    # --- URL Mode UI ---
    with gr.Column(visible=True) as url_mode_ui:
        url_input = gr.Textbox(
            label="Starting Index HTML URL",
            placeholder="e.g., https://dghs-imgutils.deepghs.org/main/index.html"
        )
        restrict_path_checkbox = gr.Checkbox(
            label="Restrict crawl to starting path structure (e.g., if start is '/main/index.html', only crawl '/main/...' URLs)",
            value=True # Default to restricting path
        )

    # --- HTML Text Mode UI ---
    with gr.Column(visible=False) as text_mode_ui:
        html_text_input = gr.Textbox(
            label="Paste HTML Source Code Here",
            lines=10, # Give it a decent initial size
            placeholder="<html><body><h1>Title</h1><p>This is a paragraph.</p></body></html>"
        )

    # --- Common Options ---
    with gr.Row():
        aggressive_md_checkbox = gr.Checkbox(
            label="Aggressive Markdown conversion (disable raw HTML, use ATX headers)",
            value=True # Default to aggressive conversion
        )
    
    with gr.Row():
        start_button = gr.Button("Start Conversion", variant="primary")

    # --- URL Mode Outputs ---
    with gr.Column(visible=True) as url_mode_outputs:
        log_output = gr.Textbox(label="Progress Logs", lines=15, interactive=False, show_copy_button=True)
        zip_output = gr.File(label="Download Markdown Archive (ZIP)")

    # --- HTML Text Mode Outputs ---
    with gr.Column(visible=False) as text_mode_outputs:
        gr.Markdown("---")
        gr.Markdown("### Markdown Conversion Result")
        md_output_display = gr.Markdown(label="Preview") # Preview the result
        md_output_file = gr.File(label="Download Markdown File (.md)") # Download the single file

    # --- UI Logic to switch between modes ---
    def update_ui_visibility(input_type):
        is_url_mode = (input_type == "Convert from URL")
        return {
            url_mode_ui: gr.update(visible=is_url_mode),
            text_mode_ui: gr.update(visible=not is_url_mode),
            url_mode_outputs: gr.update(visible=is_url_mode),
            text_mode_outputs: gr.update(visible=not is_url_mode),
        }

    input_type_radio.change(
        fn=update_ui_visibility,
        inputs=input_type_radio,
        outputs=[url_mode_ui, text_mode_ui, url_mode_outputs, text_mode_outputs]
    )

    # --- Button click event wiring ---
    start_button.click(
        fn=process_conversion_request,
        inputs=[
            input_type_radio,
            url_input,
            html_text_input,
            restrict_path_checkbox,
            aggressive_md_checkbox
        ],
        # The function now needs to update all possible outputs
        outputs=[
            log_output,
            zip_output,
            md_output_display,
            md_output_file
        ],
        show_progress="full"
    )

# --- Launch App ---
if __name__ == "__main__":
    demo.queue()
    demo.launch(inbrowser=True)
